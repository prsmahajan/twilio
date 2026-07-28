import csv
import io
import os
import re
import sqlite3
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, send_from_directory, g, abort
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Dial
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static/dist', static_url_path='')


# ── Config ────────────────────────────────────────────────────────────────────

def _require_env(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            "Set it in .env (local) or the service env vars (deploy)."
        )
    return val


TWILIO_ACCOUNT_SID  = _require_env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = _require_env("TWILIO_AUTH_TOKEN")
TWILIO_API_KEY      = _require_env("TWILIO_API_KEY")
TWILIO_API_SECRET   = _require_env("TWILIO_API_SECRET")
TWILIO_APP_SID      = _require_env("TWILIO_APP_SID")
TWILIO_PHONE_NUMBER = _require_env("TWILIO_PHONE_NUMBER")

# Identity the browser client registers as. Inbound calls are routed here.
CLIENT_IDENTITY = os.environ.get("CLIENT_IDENTITY", "dialer-user")

# Optional. When set, SMS is sent through this Messaging Service rather than the
# bare number, so it inherits the service's A2P 10DLC campaign registration.
TWILIO_MESSAGING_SERVICE_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "").strip()

# Public https base URL of this app, e.g. https://dialer.onrender.com
# Used to build status-callback URLs and to verify Twilio signatures behind a proxy.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Default country code applied to bare national numbers.
DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "+1")

# Digits in a national number, used to decide whether a bare string already
# carries a country code. 10 suits US/CA/IN; set it per your default country.
NATIONAL_NUMBER_LENGTH = int(os.environ.get("NATIONAL_NUMBER_LENGTH", "10"))

# Comma-separated E.164 prefixes this app may dial. "*" allows every country.
#
# With "*" the geographic toll-fraud backstop is off, and the only remaining
# controls are Twilio's own Voice Geographic Permissions (Console > Voice >
# Settings > Geo permissions) and your account balance. Enable just the
# countries you actually call there.
ALLOWED_PREFIXES = tuple(
    p.strip() for p in os.environ.get("ALLOWED_PREFIXES", "*").split(",") if p.strip()
)
ALLOW_ALL_COUNTRIES = "*" in ALLOWED_PREFIXES or not ALLOWED_PREFIXES

# Prefixes that are always refused even if they match an allowed prefix.
# US premium-rate / payphone ranges by default.
BLOCKED_PREFIXES = tuple(
    p.strip() for p in os.environ.get(
        "BLOCKED_PREFIXES", "+1900,+1976,+1809,+1829,+1849"
    ).split(",") if p.strip()
)

# Escape hatch for local testing without a public tunnel. Never set this in production.
VALIDATE_TWILIO_SIGNATURE = os.environ.get(
    "VALIDATE_TWILIO_SIGNATURE", "true"
).lower() not in ("0", "false", "no")

DB_PATH = os.environ.get("DB_PATH", "dialer.db")

_client = None


def get_client():
    global _client
    if _client is None:
        _client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _client


# ── Database ──────────────────────────────────────────────────────────────────

# Every table carries owner_id. Today it is always DEFAULT_OWNER; when multiple
# agents are added later it becomes a real foreign key without a schema rewrite.
DEFAULT_OWNER = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_log (
    call_sid     TEXT PRIMARY KEY,
    parent_sid   TEXT,
    direction    TEXT,
    from_number  TEXT,
    to_number    TEXT,
    status       TEXT,
    duration     INTEGER,
    lead_id      INTEGER,
    campaign_id  INTEGER,
    disposition  TEXT,
    note         TEXT,
    owner_id     TEXT DEFAULT 'default',
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT DEFAULT '',
    phone        TEXT NOT NULL,
    email        TEXT DEFAULT '',
    company      TEXT DEFAULT '',
    notes        TEXT DEFAULT '',
    tags         TEXT DEFAULT '',
    status       TEXT DEFAULT 'new',
    dnc          INTEGER DEFAULT 0,
    source       TEXT DEFAULT '',
    owner_id     TEXT DEFAULT 'default',
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(phone, owner_id)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    status           TEXT DEFAULT 'idle',
    gap_seconds      INTEGER DEFAULT 2,
    max_attempts     INTEGER DEFAULT 1,
    retry_no_answer  INTEGER DEFAULT 0,
    owner_id         TEXT DEFAULT 'default',
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaign_leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  INTEGER NOT NULL,
    lead_id      INTEGER NOT NULL,
    position     INTEGER DEFAULT 0,
    attempts     INTEGER DEFAULT 0,
    last_status  TEXT DEFAULT '',
    disposition  TEXT DEFAULT '',
    done         INTEGER DEFAULT 0,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(campaign_id, lead_id),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
    FOREIGN KEY (lead_id)     REFERENCES leads(id)     ON DELETE CASCADE
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_call_log_to ON call_log(to_number);
CREATE INDEX IF NOT EXISTS idx_call_log_created ON call_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_log_lead ON call_log(lead_id);
CREATE INDEX IF NOT EXISTS idx_call_log_campaign ON call_log(campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_cl_campaign ON campaign_leads(campaign_id, position);
"""


def _configure(conn):
    """Make SQLite safe to share between gunicorn workers.

    Each worker is a separate process with its own connection. Without WAL a
    single writer blocks all readers, and without a busy timeout a concurrent
    write fails outright with "database is locked" instead of waiting.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = _configure(sqlite3.connect(DB_PATH, timeout=10))
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added to call_log after the first release. CREATE TABLE IF NOT EXISTS
# will not add them to an existing database, so they are applied explicitly.
_CALL_LOG_ADDED_COLUMNS = [
    ("lead_id",     "INTEGER"),
    ("campaign_id", "INTEGER"),
    ("disposition", "TEXT"),
    ("note",        "TEXT"),
    ("owner_id",    "TEXT DEFAULT 'default'"),
]


def init_db():
    # Create the parent directory so DB_PATH can point at a mounted disk.
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)

    conn = _configure(sqlite3.connect(DB_PATH, timeout=10))

    # Order matters: tables, then the column migration, then indexes. The new
    # indexes reference columns that an existing database does not have yet, so
    # creating them before the ALTER TABLE fails with "no such column".
    conn.executescript(SCHEMA)

    existing = {r[1] for r in conn.execute("PRAGMA table_info(call_log)")}
    for col, decl in _CALL_LOG_ADDED_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE call_log ADD COLUMN {col} {decl}")

    conn.executescript(INDEXES)
    conn.commit()
    conn.close()


init_db()


# ── Phone number handling ─────────────────────────────────────────────────────

def normalize_number(raw):
    """Return an E.164 string, or None if the input cannot be interpreted.

    A leading '+' is optional. Numbers may simply start with their country
    code — 15551234567, 919876543210, 442071234567 all work, as do the
    formatted variants '(555) 123-4567' and '1-555-123-4567'.

    Disambiguation rule: a bare NATIONAL_NUMBER_LENGTH-digit string (10 by
    default) is treated as a national number and gets DEFAULT_COUNTRY_CODE
    prepended. Anything longer is assumed to already carry its country code.

    The consequence worth knowing: a country whose full E.164 length is also
    10 digits (Iceland's +354 555 1234, say) would be misread as national.
    Type a '+' for those. The UI always shows the resolved E.164 before dialing.

    Client identities ('client:foo') pass through untouched.
    """
    if not raw:
        return None
    raw = raw.strip()

    if raw.startswith("client:"):
        return raw

    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None

    if has_plus:
        candidate = "+" + digits
    elif len(digits) == NATIONAL_NUMBER_LENGTH:
        candidate = DEFAULT_COUNTRY_CODE + digits
    elif len(digits) > NATIONAL_NUMBER_LENGTH:
        # Long enough to carry a country code already.
        candidate = "+" + digits
    else:
        # Too short to be a dialable international number.
        return None

    # E.164 allows at most 15 digits after the +.
    if not re.fullmatch(r"\+[1-9]\d{7,14}", candidate):
        return None
    return candidate


def is_dialable(e164):
    """Allowlist check. Runs after normalization, before any billable action.

    The blocklist applies even when all countries are allowed, so premium-rate
    ranges stay refused regardless of configuration.
    """
    if not e164:
        return False
    if e164.startswith("client:"):
        return True
    if any(e164.startswith(p) for p in BLOCKED_PREFIXES):
        return False
    if ALLOW_ALL_COUNTRIES:
        return True
    return any(e164.startswith(p) for p in ALLOWED_PREFIXES)


# ── Twilio webhook signature validation ───────────────────────────────────────

def _external_url():
    """The URL Twilio used, reconstructed correctly behind a TLS-terminating proxy.

    Twilio computes its signature over the exact URL it requested. Render (and any
    reverse proxy) hands Flask an http:// URL, so request.url would not match and
    every signature would fail. PUBLIC_BASE_URL is the reliable fix; the
    X-Forwarded-Proto fallback covers the case where it is not configured.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + request.full_path.rstrip("?")
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return request.url.replace("http://", proto + "://", 1)


def twilio_webhook(f):
    """Reject any request to a TwiML endpoint that Twilio did not sign.

    Without this, an unauthenticated POST can drive an outbound call billed to
    this account. The signature is an HMAC over the URL and POST params keyed by
    the account auth token, so only Twilio can produce a valid one.
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        if VALIDATE_TWILIO_SIGNATURE:
            validator = RequestValidator(TWILIO_AUTH_TOKEN)
            signature = request.headers.get("X-Twilio-Signature", "")
            if not validator.validate(_external_url(), request.form, signature):
                app.logger.warning(
                    "Rejected unsigned request to %s from %s",
                    request.path, request.remote_addr,
                )
                abort(403)
        return f(*args, **kwargs)
    return wrapper


def _status_callback_url(path):
    return (PUBLIC_BASE_URL + path) if PUBLIC_BASE_URL else None


# ── Static ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/healthz')
def healthz():
    """Liveness probe. Touches the database so a broken disk mount fails loudly."""
    try:
        get_db().execute("SELECT 1").fetchone()
        return jsonify({"ok": True, "db": DB_PATH})
    except Exception as e:
        app.logger.exception("health check failed")
        return jsonify({"ok": False, "error": str(e)}), 503


# ── Voice token ───────────────────────────────────────────────────────────────

TOKEN_TTL = 3600


@app.route("/token", methods=["GET"])
def token():
    try:
        grant = VoiceGrant(
            outgoing_application_sid=TWILIO_APP_SID,
            incoming_allow=True,
        )
        access_token = AccessToken(
            TWILIO_ACCOUNT_SID, TWILIO_API_KEY, TWILIO_API_SECRET,
            identity=CLIENT_IDENTITY, ttl=TOKEN_TTL,
        )
        access_token.add_grant(grant)
        return jsonify({
            "token":    access_token.to_jwt(),
            "identity": CLIENT_IDENTITY,
            "ttl":      TOKEN_TTL,
        })
    except Exception as e:
        app.logger.exception("token generation failed")
        return jsonify({"error": str(e)}), 500


# ── Outbound TwiML webhook ────────────────────────────────────────────────────

@app.route("/connect", methods=["POST"])
@twilio_webhook
def connect():
    raw_to   = request.form.get("To", "")
    response = VoiceResponse()

    to_number = normalize_number(raw_to)
    if not to_number:
        response.say("Invalid destination number.")
        return str(response), 200, {"Content-Type": "text/xml"}

    # An inbound call misrouted to this endpoint arrives with To set to our own
    # Twilio number. Dialing it would call ourselves in a loop and bill for it.
    # This is the safety net for a number pointed at the TwiML app instead of
    # /incoming; route it to the browser client, which is what was intended.
    if to_number == TWILIO_PHONE_NUMBER:
        app.logger.warning(
            "Inbound call reached /connect — number is pointed at the TwiML app "
            "rather than /incoming. Routing to the client instead of self-dialing."
        )
        dial = Dial(caller_id=request.form.get("From", "") or TWILIO_PHONE_NUMBER,
                    answer_on_bridge=True, timeout=30)
        dial.client(CLIENT_IDENTITY)
        response.append(dial)
        return str(response), 200, {"Content-Type": "text/xml"}

    if not is_dialable(to_number):
        app.logger.warning("Blocked disallowed destination %s", to_number)
        response.say("This destination is not permitted.")
        return str(response), 200, {"Content-Type": "text/xml"}

    # A lead marked do-not-call must be unreachable from every path, including a
    # manually typed number, not just excluded from campaign queues.
    row = get_db().execute(
        "SELECT dnc FROM leads WHERE phone = ? AND owner_id = ?",
        (to_number, DEFAULT_OWNER),
    ).fetchone()
    if row and row["dnc"]:
        app.logger.warning("Blocked do-not-call number %s", to_number)
        response.say("This number is on your do not call list.")
        return str(response), 200, {"Content-Type": "text/xml"}

    dial = Dial(caller_id=TWILIO_PHONE_NUMBER, answer_on_bridge=True, timeout=20)
    cb = _status_callback_url("/call_status")
    if cb:
        dial.number(
            to_number,
            status_callback=cb,
            status_callback_event="initiated ringing answered completed",
            status_callback_method="POST",
        )
    else:
        dial.number(to_number)
    response.append(dial)
    return str(response), 200, {"Content-Type": "text/xml"}


# ── Inbound TwiML webhook ─────────────────────────────────────────────────────

@app.route("/incoming", methods=["POST"])
@twilio_webhook
def incoming():
    """Route a call to the Twilio number into the browser client."""
    from_number = request.form.get("From", "")
    call_sid    = request.form.get("CallSid", "")

    if call_sid:
        _record_call(
            call_sid=call_sid,
            direction="inbound",
            from_number=from_number,
            to_number=TWILIO_PHONE_NUMBER,
            status="ringing",
        )

    response = VoiceResponse()
    dial = Dial(caller_id=from_number, answer_on_bridge=True, timeout=30)
    dial.client(CLIENT_IDENTITY)
    response.append(dial)
    return str(response), 200, {"Content-Type": "text/xml"}


# ── Call status callback ──────────────────────────────────────────────────────

@app.route("/call_status", methods=["POST"])
@twilio_webhook
def call_status():
    call_sid = request.form.get("CallSid", "")
    if not call_sid:
        return ("", 204)

    duration = request.form.get("CallDuration") or request.form.get("DialCallDuration")
    _record_call(
        call_sid=call_sid,
        parent_sid=request.form.get("ParentCallSid", ""),
        direction=request.form.get("Direction", ""),
        from_number=request.form.get("From", ""),
        to_number=request.form.get("To", ""),
        status=request.form.get("CallStatus", ""),
        duration=int(duration) if duration and duration.isdigit() else None,
    )
    _sync_campaign_progress(call_sid, request.form.get("CallStatus", ""))
    return ("", 204)


TERMINAL_STATUSES = ("completed", "busy", "no-answer", "failed", "canceled")


def _sync_campaign_progress(call_sid, status):
    """Mirror a call's final status onto its campaign row.

    A manual disposition, if the user records one, takes precedence and is what
    marks the row done; this only handles the case where they never pick one.
    """
    if status not in TERMINAL_STATUSES:
        return

    db  = get_db()
    row = db.execute(
        "SELECT lead_id, campaign_id, to_number FROM call_log WHERE call_sid = ?",
        (call_sid,),
    ).fetchone()
    if not row:
        return

    lead_id = row["lead_id"]
    # Late-binding fallback: if the browser never linked the call, match on number.
    if not lead_id and row["to_number"]:
        lead = db.execute(
            "SELECT id FROM leads WHERE phone = ? AND owner_id = ?",
            (row["to_number"], DEFAULT_OWNER),
        ).fetchone()
        if lead:
            lead_id = lead["id"]
            db.execute("UPDATE call_log SET lead_id = ? WHERE call_sid = ?",
                       (lead_id, call_sid))

    if not (lead_id and row["campaign_id"]):
        db.commit()
        return

    campaign = db.execute(
        "SELECT max_attempts, retry_no_answer FROM campaigns WHERE id = ?",
        (row["campaign_id"],),
    ).fetchone()
    cl = db.execute(
        "SELECT attempts, disposition FROM campaign_leads WHERE campaign_id = ? AND lead_id = ?",
        (row["campaign_id"], lead_id),
    ).fetchone()

    if campaign and cl:
        retryable = (
            campaign["retry_no_answer"]
            and status in ("no-answer", "busy")
            and cl["attempts"] < campaign["max_attempts"]
        )
        done = 0 if retryable else 1
        db.execute(
            """UPDATE campaign_leads SET last_status = ?, done = ?,
                                         updated_at = CURRENT_TIMESTAMP
               WHERE campaign_id = ? AND lead_id = ?""",
            (status, done, row["campaign_id"], lead_id),
        )
    db.commit()


def _record_call(call_sid, direction="", from_number="", to_number="",
                 status="", duration=None, parent_sid=""):
    db = get_db()
    db.execute(
        """
        INSERT INTO call_log (call_sid, parent_sid, direction, from_number,
                              to_number, status, duration)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(call_sid) DO UPDATE SET
            status     = excluded.status,
            duration   = COALESCE(excluded.duration, call_log.duration),
            updated_at = CURRENT_TIMESTAMP
        """,
        (call_sid, parent_sid, direction, from_number, to_number, status, duration),
    )
    db.commit()


# ── Call dispositions ─────────────────────────────────────────────────────────

DISPOSITIONS = {
    "completed":  "answered",
    "busy":       "busy",
    "no-answer":  "no-answer",
    "failed":     "failed",
    "canceled":   "canceled",
}


@app.route("/dispositions", methods=["GET"])
def dispositions():
    """Outcome per dialed number, most recent first. Powers the autodialer results."""
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = get_db().execute(
        """
        SELECT to_number, status, duration, updated_at
        FROM call_log
        WHERE status IN ('completed','busy','no-answer','failed','canceled')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return jsonify([
        {
            "number":      r["to_number"],
            "status":      r["status"],
            "disposition": DISPOSITIONS.get(r["status"], r["status"]),
            "duration":    r["duration"],
            "date":        r["updated_at"],
        }
        for r in rows
    ])


# ── SMS threads (one entry per contact, most recent message) ──────────────────

@app.route("/threads", methods=["GET"])
def threads():
    try:
        client   = get_client()
        sent     = list(client.messages.list(from_=TWILIO_PHONE_NUMBER, limit=100))
        received = list(client.messages.list(to=TWILIO_PHONE_NUMBER,   limit=100))

        thread_map = {}
        for msg in sent + received:
            msg_from = getattr(msg, 'from_', '') or ''
            contact  = msg.to if msg_from == TWILIO_PHONE_NUMBER else msg_from
            date     = msg.date_sent
            existing = thread_map.get(contact)
            if existing is None or (date and existing["_dt"] and date > existing["_dt"]):
                thread_map[contact] = {
                    "contact":   contact,
                    "body":      msg.body or "",
                    "date":      date.isoformat() if date else "",
                    "direction": "outbound" if msg_from == TWILIO_PHONE_NUMBER else "inbound",
                    "_dt":       date,
                }

        result = sorted(
            [{k: v for k, v in t.items() if k != "_dt"} for t in thread_map.values()],
            key=lambda x: x["date"],
            reverse=True,
        )
        return jsonify(result)
    except Exception as e:
        app.logger.exception("threads failed")
        return jsonify({"error": str(e)}), 500


# ── SMS conversation with a single contact ────────────────────────────────────

@app.route("/messages", methods=["GET"])
def messages():
    contact = normalize_number(request.args.get("contact", ""))
    if not contact:
        return jsonify([])
    try:
        client   = get_client()
        sent     = list(client.messages.list(from_=TWILIO_PHONE_NUMBER, to=contact, limit=50))
        received = list(client.messages.list(from_=contact, to=TWILIO_PHONE_NUMBER, limit=50))
        all_msgs = []
        for msg in sent + received:
            msg_from = getattr(msg, 'from_', '') or ''
            all_msgs.append({
                "sid":       msg.sid,
                "body":      msg.body or "",
                "direction": "outbound" if msg_from == TWILIO_PHONE_NUMBER else "inbound",
                "date":      msg.date_sent.isoformat() if msg.date_sent else "",
            })
        all_msgs.sort(key=lambda x: x["date"])
        return jsonify(all_msgs)
    except Exception as e:
        app.logger.exception("messages failed")
        return jsonify({"error": str(e)}), 500


# ── Send SMS ──────────────────────────────────────────────────────────────────

@app.route("/send_sms", methods=["POST"])
def send_sms():
    data = request.get_json(silent=True) or {}
    to   = normalize_number(data.get("to", ""))
    body = (data.get("body") or "").strip()

    if not to:
        return jsonify({"error": "invalid destination number"}), 400
    if not body:
        return jsonify({"error": "message body is empty"}), 400
    if not is_dialable(to):
        return jsonify({"error": "destination not permitted"}), 403

    try:
        client = get_client()
        # Prefer the Messaging Service when configured: it carries the A2P 10DLC
        # campaign registration that US carriers require, and handles routing.
        if TWILIO_MESSAGING_SERVICE_SID:
            msg = client.messages.create(
                to=to, messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID, body=body
            )
        else:
            msg = client.messages.create(to=to, from_=TWILIO_PHONE_NUMBER, body=body)
        return jsonify({"sid": msg.sid, "status": msg.status})
    except Exception as e:
        app.logger.exception("send_sms failed")
        # Twilio surfaces the actionable reason in .code — 30034 means the number
        # has no registered A2P campaign, which no amount of retrying will fix.
        code = getattr(e, "code", None)
        return jsonify({"error": str(e), "twilio_code": code}), 500


# ── Recent calls ──────────────────────────────────────────────────────────────

@app.route("/recent", methods=["GET"])
def recent():
    """Call history, newest first, with lead names resolved where known.

    Two Twilio quirks are handled here:

    1. CallInstance exposes the caller as `_from`, not `from_` (MessageInstance
       uses `from_`). Reading `from_` silently yields nothing, which blanked the
       caller on every row.
    2. A browser-placed call creates two legs: a parent whose From is
       'client:<identity>' and whose To is empty, and a child that carries the
       real dialed number. Only the leg with a real counterparty is useful, so
       client legs are dropped rather than rendered as empty rows.
    """
    try:
        client   = get_client()
        outbound = list(client.calls.list(from_=TWILIO_PHONE_NUMBER, limit=50))
        inbound  = list(client.calls.list(to=TWILIO_PHONE_NUMBER,   limit=50))

        seen, rows = set(), []
        for call in outbound + inbound:
            if call.sid in seen:
                continue
            seen.add(call.sid)

            frm = getattr(call, "_from", "") or ""
            to  = getattr(call, "to", "") or ""

            # Drop the browser leg — no real counterparty on it.
            if frm.startswith("client:") or not (frm and to):
                continue

            is_inbound   = (call.direction or "").startswith("inbound")
            counterparty = frm if is_inbound else to

            rows.append({
                "sid":       call.sid,
                "to":        to,
                "from_":     frm,
                "number":    counterparty,
                "direction": call.direction,
                "status":    call.status,
                "duration":  call.duration,
                "date":      call.date_created.isoformat() if call.date_created else "",
            })

        # Resolve names in one query rather than per row.
        numbers = {r["number"] for r in rows if r["number"]}
        names = {}
        if numbers:
            marks = ",".join("?" * len(numbers))
            for lead in get_db().execute(
                f"SELECT phone, name FROM leads WHERE owner_id = ? AND phone IN ({marks})",
                (DEFAULT_OWNER, *numbers),
            ):
                if lead["name"]:
                    names[lead["phone"]] = lead["name"]

        for r in rows:
            r["name"] = names.get(r["number"], "")

        rows.sort(key=lambda x: x["date"], reverse=True)
        return jsonify(rows)
    except Exception as e:
        app.logger.exception("recent failed")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# CRM — leads
# ══════════════════════════════════════════════════════════════════════════════

def _lead_row(r):
    return {
        "id":      r["id"],
        "name":    r["name"],
        "phone":   r["phone"],
        "email":   r["email"],
        "company": r["company"],
        "notes":   r["notes"],
        "tags":    r["tags"],
        "status":  r["status"],
        "dnc":     bool(r["dnc"]),
        "source":  r["source"],
        "created": r["created_at"],
    }


@app.route("/api/leads", methods=["GET"])
def list_leads():
    q       = (request.args.get("q") or "").strip()
    status  = (request.args.get("status") or "").strip()
    limit   = min(int(request.args.get("limit", 200)), 1000)
    offset  = int(request.args.get("offset", 0))

    sql    = "SELECT * FROM leads WHERE owner_id = ?"
    params = [DEFAULT_OWNER]

    if q:
        sql += " AND (name LIKE ? OR phone LIKE ? OR company LIKE ? OR email LIKE ?)"
        params += [f"%{q}%"] * 4
    if status:
        sql += " AND status = ?"
        params.append(status)

    sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    rows  = get_db().execute(sql, params).fetchall()
    total = get_db().execute(
        "SELECT COUNT(*) c FROM leads WHERE owner_id = ?", (DEFAULT_OWNER,)
    ).fetchone()["c"]
    return jsonify({"leads": [_lead_row(r) for r in rows], "total": total})


@app.route("/api/leads", methods=["POST"])
def create_lead():
    data  = request.get_json(silent=True) or {}
    phone = normalize_number(data.get("phone", ""))
    if not phone:
        return jsonify({"error": "invalid phone number"}), 400

    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO leads (name, phone, email, company, notes, tags, status, source, owner_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (data.get("name") or "").strip(),
                phone,
                (data.get("email") or "").strip(),
                (data.get("company") or "").strip(),
                (data.get("notes") or "").strip(),
                (data.get("tags") or "").strip(),
                (data.get("status") or "new").strip(),
                (data.get("source") or "manual").strip(),
                DEFAULT_OWNER,
            ),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "a lead with that phone number already exists"}), 409

    row = db.execute("SELECT * FROM leads WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(_lead_row(row)), 201


@app.route("/api/leads/<int:lead_id>", methods=["PATCH"])
def update_lead(lead_id):
    data = request.get_json(silent=True) or {}
    allowed = ("name", "email", "company", "notes", "tags", "status", "dnc")

    sets, params = [], []
    for field in allowed:
        if field in data:
            sets.append(f"{field} = ?")
            params.append(int(bool(data[field])) if field == "dnc" else data[field])

    if "phone" in data:
        phone = normalize_number(data["phone"])
        if not phone:
            return jsonify({"error": "invalid phone number"}), 400
        sets.append("phone = ?")
        params.append(phone)

    if not sets:
        return jsonify({"error": "nothing to update"}), 400

    sets.append("updated_at = CURRENT_TIMESTAMP")
    params += [lead_id, DEFAULT_OWNER]

    db = get_db()
    db.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id = ? AND owner_id = ?", params)
    db.commit()

    row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_lead_row(row))


@app.route("/api/leads/<int:lead_id>", methods=["DELETE"])
def delete_lead(lead_id):
    db = get_db()
    _detach_lead_references(db, [lead_id])
    db.execute("DELETE FROM leads WHERE id = ? AND owner_id = ?", (lead_id, DEFAULT_OWNER))
    db.commit()
    return ("", 204)


def _detach_lead_references(db, lead_ids):
    """Unlink a lead from campaigns and call history before deleting it.

    Call history is kept and its lead_id nulled rather than deleted — the calls
    happened, and billing/analytics should still reflect them.
    """
    if not lead_ids:
        return
    marks = ",".join("?" * len(lead_ids))
    db.execute(f"DELETE FROM campaign_leads WHERE lead_id IN ({marks})", tuple(lead_ids))
    db.execute(f"UPDATE call_log SET lead_id = NULL WHERE lead_id IN ({marks})", tuple(lead_ids))


@app.route("/api/leads", methods=["DELETE"])
def delete_all_leads():
    """Bulk-delete leads. Irreversible, so it requires explicit confirmation.

    Leads on the do-not-call list are preserved by default. Deleting them would
    destroy the record of who asked not to be contacted, and they would silently
    become dialable again on the next import.
    """
    data     = request.get_json(silent=True) or {}
    confirm  = (data.get("confirm") or request.args.get("confirm") or "").strip()
    keep_dnc = data.get("keep_dnc", True)

    if confirm != "DELETE":
        return jsonify({"error": 'confirmation required: send {"confirm": "DELETE"}'}), 400

    db  = get_db()
    sql = "SELECT id FROM leads WHERE owner_id = ?"
    if keep_dnc:
        sql += " AND dnc = 0"
    ids = [r["id"] for r in db.execute(sql, (DEFAULT_OWNER,))]

    if not ids:
        return jsonify({"deleted": 0, "kept_dnc": 0})

    _detach_lead_references(db, ids)
    marks = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM leads WHERE id IN ({marks})", tuple(ids))
    db.commit()

    kept = db.execute(
        "SELECT COUNT(*) c FROM leads WHERE owner_id = ? AND dnc = 1", (DEFAULT_OWNER,)
    ).fetchone()["c"]

    app.logger.warning("Bulk-deleted %d leads (kept %d do-not-call)", len(ids), kept)
    return jsonify({"deleted": len(ids), "kept_dnc": kept})


@app.route("/api/leads/<int:lead_id>/calls", methods=["GET"])
def lead_calls(lead_id):
    rows = get_db().execute(
        """SELECT call_sid, status, disposition, note, duration, updated_at
           FROM call_log WHERE lead_id = ? ORDER BY updated_at DESC LIMIT 50""",
        (lead_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ══════════════════════════════════════════════════════════════════════════════
# Lead import — CSV text, uploaded file, or a shared Google Sheet
# ══════════════════════════════════════════════════════════════════════════════

# Header aliases, so an arbitrary spreadsheet maps onto our fields.
COLUMN_ALIASES = {
    "phone":   {"phone", "phone number", "phonenumber", "mobile", "cell", "number",
                "contact", "contact number", "tel", "telephone", "msisdn"},
    "name":    {"name", "full name", "fullname", "lead name", "contact name",
                "first name", "firstname", "customer"},
    "email":   {"email", "e-mail", "email address", "mail"},
    "company": {"company", "organisation", "organization", "org", "business", "account"},
    "notes":   {"notes", "note", "comment", "comments", "remark", "remarks"},
    "tags":    {"tags", "tag", "label", "labels", "segment"},
}


def _map_headers(fieldnames):
    """Map a spreadsheet's headers onto lead fields, case/space insensitive."""
    mapping = {}
    for raw in fieldnames or []:
        key = re.sub(r"[\s_]+", " ", (raw or "").strip().lower())
        for field, aliases in COLUMN_ALIASES.items():
            if key in aliases and field not in mapping:
                mapping[field] = raw
                break
    return mapping


def sheet_csv_url(url):
    """Convert a Google Sheets URL into its CSV export URL.

    Only docs.google.com is accepted. This endpoint fetches a user-supplied URL
    server-side, so allowing arbitrary hosts would turn it into an SSRF vector
    against the internal network and cloud metadata endpoints.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "docs.google.com":
        return None

    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", parsed.path)
    if not m:
        return None
    sheet_id = m.group(1)

    gid = "0"
    gm = re.search(r"[#&?]gid=(\d+)", url)
    if gm:
        gid = gm.group(1)

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _import_rows(reader, source):
    """Insert normalized rows, returning a per-row report."""
    mapping = _map_headers(reader.fieldnames)
    if "phone" not in mapping:
        return None, (
            "Could not find a phone column. Expected a header like "
            "'phone', 'mobile', 'number' or 'contact'."
        )

    db = get_db()
    imported = skipped = duplicates = 0
    errors = []

    for i, row in enumerate(reader, start=2):   # row 1 is the header
        raw_phone = (row.get(mapping["phone"]) or "").strip()
        if not raw_phone:
            continue

        phone = normalize_number(raw_phone)
        if not phone:
            skipped += 1
            if len(errors) < 20:
                errors.append(f"row {i}: could not read '{raw_phone}' as a phone number")
            continue

        vals = {
            f: (row.get(mapping[f]) or "").strip() if f in mapping else ""
            for f in ("name", "email", "company", "notes", "tags")
        }

        try:
            db.execute(
                """INSERT INTO leads (name, phone, email, company, notes, tags, source, owner_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (vals["name"], phone, vals["email"], vals["company"],
                 vals["notes"], vals["tags"], source, DEFAULT_OWNER),
            )
            imported += 1
        except sqlite3.IntegrityError:
            duplicates += 1

    db.commit()
    return {
        "imported":   imported,
        "skipped":    skipped,
        "duplicates": duplicates,
        "errors":     errors,
    }, None


@app.route("/api/leads/import", methods=["POST"])
def import_leads():
    """Import from pasted CSV text, an uploaded file, or a Google Sheets URL."""
    # Uploaded file
    if request.files.get("file"):
        f = request.files["file"]
        try:
            text = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return jsonify({"error": "file must be UTF-8 encoded CSV"}), 400
        report, err = _import_rows(csv.DictReader(io.StringIO(text)), f.filename or "upload")
        return (jsonify({"error": err}), 400) if err else jsonify(report)

    data = request.get_json(silent=True) or {}

    # Google Sheet
    if data.get("sheet_url"):
        csv_url = sheet_csv_url(data["sheet_url"].strip())
        if not csv_url:
            return jsonify({
                "error": "Not a Google Sheets URL. Paste the full "
                         "https://docs.google.com/spreadsheets/d/... link."
            }), 400
        try:
            resp = requests.get(csv_url, timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            return jsonify({"error": f"could not fetch sheet: {e}"}), 502

        if resp.status_code != 200 or "text/csv" not in resp.headers.get("Content-Type", ""):
            return jsonify({
                "error": "Sheet is not publicly readable. In Google Sheets use "
                         "Share > General access > Anyone with the link (Viewer)."
            }), 403

        report, err = _import_rows(
            csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))), "google-sheet"
        )
        return (jsonify({"error": err}), 400) if err else jsonify(report)

    # Pasted CSV text
    if data.get("csv"):
        report, err = _import_rows(csv.DictReader(io.StringIO(data["csv"])), "csv-paste")
        return (jsonify({"error": err}), 400) if err else jsonify(report)

    return jsonify({"error": "provide csv, sheet_url, or a file upload"}), 400


# ══════════════════════════════════════════════════════════════════════════════
# Campaigns
# ══════════════════════════════════════════════════════════════════════════════

def _campaign_progress(db, campaign_id):
    r = db.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN done = 1 THEN 1 ELSE 0 END) done
           FROM campaign_leads WHERE campaign_id = ?""",
        (campaign_id,),
    ).fetchone()
    return {"total": r["total"] or 0, "done": r["done"] or 0}


@app.route("/api/campaigns", methods=["GET"])
def list_campaigns():
    db   = get_db()
    rows = db.execute(
        "SELECT * FROM campaigns WHERE owner_id = ? ORDER BY updated_at DESC",
        (DEFAULT_OWNER,),
    ).fetchall()
    out = []
    for r in rows:
        c = dict(r)
        c.update(_campaign_progress(db, r["id"]))
        out.append(c)
    return jsonify(out)


@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    db  = get_db()
    cur = db.execute(
        """INSERT INTO campaigns (name, gap_seconds, max_attempts, retry_no_answer, owner_id)
           VALUES (?, ?, ?, ?, ?)""",
        (name, int(data.get("gap_seconds", 2)), int(data.get("max_attempts", 1)),
         int(bool(data.get("retry_no_answer", False))), DEFAULT_OWNER),
    )
    campaign_id = cur.lastrowid

    # Membership can come from explicit lead ids or a filter over all leads.
    lead_ids = data.get("lead_ids")
    if lead_ids is None and data.get("all_leads"):
        lead_ids = [
            r["id"] for r in db.execute(
                "SELECT id FROM leads WHERE owner_id = ? AND dnc = 0", (DEFAULT_OWNER,)
            )
        ]

    for pos, lid in enumerate(lead_ids or []):
        db.execute(
            "INSERT OR IGNORE INTO campaign_leads (campaign_id, lead_id, position) VALUES (?, ?, ?)",
            (campaign_id, lid, pos),
        )
    db.commit()

    row = db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    c = dict(row)
    c.update(_campaign_progress(db, campaign_id))
    return jsonify(c), 201


@app.route("/api/campaigns/<int:cid>", methods=["DELETE"])
def delete_campaign(cid):
    db = get_db()
    db.execute("DELETE FROM campaign_leads WHERE campaign_id = ?", (cid,))
    db.execute("DELETE FROM campaigns WHERE id = ? AND owner_id = ?", (cid, DEFAULT_OWNER))
    db.commit()
    return ("", 204)


@app.route("/api/campaigns/<int:cid>/queue", methods=["GET"])
def campaign_queue(cid):
    """The dial queue, in order. Progress lives here so a refresh cannot lose it."""
    rows = get_db().execute(
        """SELECT cl.id cl_id, cl.lead_id, cl.position, cl.attempts, cl.done,
                  cl.disposition, cl.last_status,
                  l.name, l.phone, l.company, l.dnc
           FROM campaign_leads cl
           JOIN leads l ON l.id = cl.lead_id
           WHERE cl.campaign_id = ?
           ORDER BY cl.position""",
        (cid,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/campaigns/<int:cid>/reset", methods=["POST"])
def reset_campaign(cid):
    db = get_db()
    db.execute(
        """UPDATE campaign_leads
           SET done = 0, attempts = 0, disposition = '', last_status = ''
           WHERE campaign_id = ?""",
        (cid,),
    )
    db.execute("UPDATE campaigns SET status = 'idle' WHERE id = ?", (cid,))
    db.commit()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# Dispositions
# ══════════════════════════════════════════════════════════════════════════════

# Outcomes the user picks by hand after a call.
MANUAL_DISPOSITIONS = [
    {"key": "interested",     "label": "Interested",     "tone": "good"},
    {"key": "callback",       "label": "Call back",      "tone": "warn"},
    {"key": "not_interested", "label": "Not interested", "tone": "bad"},
    {"key": "voicemail",      "label": "Left voicemail", "tone": "neutral"},
    {"key": "wrong_number",   "label": "Wrong number",   "tone": "bad"},
    {"key": "dnc",            "label": "Do not call",    "tone": "bad"},
]
MANUAL_KEYS = {d["key"] for d in MANUAL_DISPOSITIONS}


@app.route("/api/disposition-options", methods=["GET"])
def disposition_options():
    return jsonify(MANUAL_DISPOSITIONS)


@app.route("/api/calls/<call_sid>/disposition", methods=["POST"])
def set_disposition(call_sid):
    data = request.get_json(silent=True) or {}
    disp = (data.get("disposition") or "").strip()
    note = (data.get("note") or "").strip()

    if disp and disp not in MANUAL_KEYS:
        return jsonify({"error": f"unknown disposition '{disp}'"}), 400

    db = get_db()
    db.execute(
        """UPDATE call_log SET disposition = ?, note = ?, updated_at = CURRENT_TIMESTAMP
           WHERE call_sid = ?""",
        (disp, note, call_sid),
    )

    row = db.execute(
        "SELECT lead_id, campaign_id FROM call_log WHERE call_sid = ?", (call_sid,)
    ).fetchone()

    if row and row["lead_id"]:
        # "Do not call" must actually prevent future dialing, not just label the row.
        if disp == "dnc":
            db.execute(
                "UPDATE leads SET dnc = 1, status = 'dnc', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row["lead_id"],),
            )
        elif disp:
            db.execute(
                "UPDATE leads SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (disp, row["lead_id"]),
            )
        if note:
            db.execute(
                """UPDATE leads
                   SET notes = TRIM(COALESCE(notes,'') || char(10) || ?),
                       updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (note, row["lead_id"]),
            )

    if row and row["campaign_id"] and row["lead_id"]:
        db.execute(
            """UPDATE campaign_leads SET disposition = ?, done = 1,
                                         updated_at = CURRENT_TIMESTAMP
               WHERE campaign_id = ? AND lead_id = ?""",
            (disp, row["campaign_id"], row["lead_id"]),
        )

    db.commit()
    return jsonify({"ok": True})


@app.route("/api/calls/link", methods=["POST"])
def link_call():
    """Attach a call to a lead/campaign as soon as the browser places it.

    The Twilio status callback only knows phone numbers, so the CRM linkage has
    to come from the client that initiated the dial.
    """
    data     = request.get_json(silent=True) or {}
    call_sid = (data.get("call_sid") or "").strip()
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    db = get_db()
    db.execute(
        """INSERT INTO call_log (call_sid, lead_id, campaign_id, to_number, direction, owner_id)
           VALUES (?, ?, ?, ?, 'outbound', ?)
           ON CONFLICT(call_sid) DO UPDATE SET
               lead_id     = COALESCE(excluded.lead_id, call_log.lead_id),
               campaign_id = COALESCE(excluded.campaign_id, call_log.campaign_id),
               updated_at  = CURRENT_TIMESTAMP""",
        (call_sid, data.get("lead_id"), data.get("campaign_id"),
         normalize_number(data.get("to", "")) or "", DEFAULT_OWNER),
    )

    if data.get("campaign_id") and data.get("lead_id"):
        db.execute(
            """UPDATE campaign_leads
               SET attempts = attempts + 1, updated_at = CURRENT_TIMESTAMP
               WHERE campaign_id = ? AND lead_id = ?""",
            (data["campaign_id"], data["lead_id"]),
        )
    db.commit()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# Analytics
# ══════════════════════════════════════════════════════════════════════════════

CONNECTED_STATUSES = ("completed", "in-progress", "answered")


@app.route("/api/analytics", methods=["GET"])
def analytics():
    days = min(int(request.args.get("days", 14)), 90)
    db   = get_db()

    totals = db.execute(
        """SELECT COUNT(*) calls,
                  SUM(CASE WHEN status IN ('completed','answered') THEN 1 ELSE 0 END) connected,
                  COALESCE(SUM(duration), 0) talk_seconds
           FROM call_log
           WHERE owner_id = ? AND created_at >= datetime('now', ?)""",
        (DEFAULT_OWNER, f"-{days} days"),
    ).fetchone()

    calls     = totals["calls"] or 0
    connected = totals["connected"] or 0

    by_day = db.execute(
        """SELECT date(created_at) day,
                  COUNT(*) calls,
                  SUM(CASE WHEN status IN ('completed','answered') THEN 1 ELSE 0 END) connected
           FROM call_log
           WHERE owner_id = ? AND created_at >= datetime('now', ?)
           GROUP BY day ORDER BY day""",
        (DEFAULT_OWNER, f"-{days} days"),
    ).fetchall()

    by_disposition = db.execute(
        """SELECT disposition, COUNT(*) n FROM call_log
           WHERE owner_id = ? AND disposition IS NOT NULL AND disposition != ''
                 AND created_at >= datetime('now', ?)
           GROUP BY disposition ORDER BY n DESC""",
        (DEFAULT_OWNER, f"-{days} days"),
    ).fetchall()

    by_status = db.execute(
        """SELECT status, COUNT(*) n FROM call_log
           WHERE owner_id = ? AND status != '' AND created_at >= datetime('now', ?)
           GROUP BY status ORDER BY n DESC""",
        (DEFAULT_OWNER, f"-{days} days"),
    ).fetchall()

    lead_totals = db.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN dnc = 1 THEN 1 ELSE 0 END) dnc
           FROM leads WHERE owner_id = ?""",
        (DEFAULT_OWNER,),
    ).fetchone()

    return jsonify({
        "days":           days,
        "calls":          calls,
        "connected":      connected,
        "connect_rate":   round(connected / calls * 100, 1) if calls else 0.0,
        "talk_seconds":   totals["talk_seconds"] or 0,
        "avg_duration":   round((totals["talk_seconds"] or 0) / connected) if connected else 0,
        "by_day":         [dict(r) for r in by_day],
        "by_disposition": [dict(r) for r in by_disposition],
        "by_status":      [dict(r) for r in by_status],
        "leads_total":    lead_totals["total"] or 0,
        "leads_dnc":      lead_totals["dnc"] or 0,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
