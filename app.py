import csv
import io
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:                                     # pragma: no cover
    ZoneInfo = None

import requests
from flask import Flask, request, jsonify, send_from_directory, g, abort, Response
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

# Hard cap on outbound message length. 160 GSM-7 characters is one SMS segment;
# past that the carrier splits the text and bills per segment, so a message that
# looks like one text quietly costs two or three. Anything longer is refused
# rather than silently truncated, because a message cut mid-sentence is worse
# than an error the sender can act on.
#
# Note this counts characters, not segments: a single emoji forces the whole
# message to UCS-2, where one segment is only 70 characters. The cap keeps the
# common case honest; it does not model every encoding.
SMS_MAX_LENGTH = max(1, int(os.environ.get("SMS_MAX_LENGTH", "160")))

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


def _flag(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── Call recording ────────────────────────────────────────────────────────────
# Off by default. Recording a two-party call without consent is illegal in
# all-party-consent jurisdictions, so this must be a deliberate choice.
RECORD_CALLS = _flag("RECORD_CALLS")

# 'record-from-answer-dual' keeps each party on its own channel, which is what
# makes a recording useful for review. Single-channel mixes them together.
RECORDING_MODE = os.environ.get("RECORDING_MODE", "record-from-answer-dual").strip()

# Played to the callee before the call connects when recording is on. Required
# for consent in two-party-consent states.
RECORDING_ANNOUNCEMENT = os.environ.get("RECORDING_ANNOUNCEMENT", "").strip()

# ── Answering machine detection ───────────────────────────────────────────────
# 'DetectMessageEnd' waits for the greeting to finish, which is what a voicemail
# drop needs. 'Enable' reports sooner but talks over the beep.
AMD_ENABLED = _flag("AMD_ENABLED")
AMD_MODE = os.environ.get("AMD_MODE", "DetectMessageEnd").strip()

# Publicly reachable audio file (mp3/wav) dropped when AMD says machine.
VOICEMAIL_AUDIO_URL = os.environ.get("VOICEMAIL_AUDIO_URL", "").strip()
# Fallback when no audio file is configured: text spoken by Twilio's TTS.
VOICEMAIL_MESSAGE = os.environ.get("VOICEMAIL_MESSAGE", "").strip()

# ── Calling-window compliance ─────────────────────────────────────────────────
# US TCPA bars telemarketing outside 8am–9pm in the CALLEE's local time. The
# window is enforced against the area code's timezone, not the server's.
QUIET_HOURS_ENFORCED = _flag("QUIET_HOURS_ENFORCED")
CALL_WINDOW_START = int(os.environ.get("CALL_WINDOW_START", "8"))
CALL_WINDOW_END = int(os.environ.get("CALL_WINDOW_END", "21"))

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

-- One row per recording Twilio produces. The audio itself stays on Twilio; we
-- keep the SID so it can be streamed back through /api/recordings/<sid>/audio
-- rather than handing the browser a URL that needs account credentials.
CREATE TABLE IF NOT EXISTS recordings (
    recording_sid TEXT PRIMARY KEY,
    call_sid      TEXT,
    lead_id       INTEGER,
    duration      INTEGER,
    channels      INTEGER DEFAULT 1,
    status        TEXT DEFAULT '',
    -- The media URL Twilio reported for this recording. Rebuilding it from
    -- TWILIO_ACCOUNT_SID 404s whenever the call ran under a subaccount, so the
    -- callback's own URL is kept and preferred.
    media_url     TEXT DEFAULT '',
    owner_id      TEXT DEFAULT 'default',
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Append-only history for a lead: calls, texts, notes, status changes, imports.
-- Kept separate from leads.notes so the record is chronological and typed.
CREATE TABLE IF NOT EXISTS activities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER,
    kind       TEXT NOT NULL,
    body       TEXT DEFAULT '',
    ref        TEXT DEFAULT '',
    owner_id   TEXT DEFAULT 'default',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
);

-- Scheduled callbacks. due_at is stored as UTC ISO8601 so overdue comparisons
-- work in SQL regardless of the agent's timezone.
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER,
    title      TEXT NOT NULL,
    due_at     TEXT NOT NULL,
    done       INTEGER DEFAULT 0,
    owner_id   TEXT DEFAULT 'default',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
);

-- Reusable SMS bodies with {{name}}-style merge fields.
CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    body       TEXT NOT NULL,
    owner_id   TEXT DEFAULT 'default',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(name, owner_id)
);

-- Locally durable SMS log. The Twilio API is still the source of truth for
-- threads, but inbound webhooks land here so auto-replies and lead timelines
-- do not depend on a round trip to Twilio.
CREATE TABLE IF NOT EXISTS sms_log (
    message_sid TEXT PRIMARY KEY,
    lead_id     INTEGER,
    direction   TEXT,
    from_number TEXT,
    to_number   TEXT,
    body        TEXT DEFAULT '',
    status      TEXT DEFAULT '',
    -- What Twilio charged, once it says so. Absent at send time: the price is
    -- attached to the message resource minutes later, so /api/costs/sync
    -- backfills it.
    price       REAL,
    price_unit  TEXT DEFAULT '',
    price_synced_at TEXT,
    owner_id    TEXT DEFAULT 'default',
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
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
CREATE INDEX IF NOT EXISTS idx_recordings_call ON recordings(call_sid);
CREATE INDEX IF NOT EXISTS idx_activities_lead ON activities(lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(done, due_at);
CREATE INDEX IF NOT EXISTS idx_tasks_lead ON tasks(lead_id);
CREATE INDEX IF NOT EXISTS idx_sms_log_lead ON sms_log(lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_line_type ON leads(line_type);
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
    ("lead_id",      "INTEGER"),
    ("campaign_id",  "INTEGER"),
    ("disposition",  "TEXT"),
    ("note",         "TEXT"),
    ("owner_id",     "TEXT DEFAULT 'default'"),
    ("answered_by",  "TEXT DEFAULT ''"),
    ("recording_sid", "TEXT DEFAULT ''"),
    ("price",         "REAL"),
    ("price_unit",    "TEXT DEFAULT ''"),
    ("price_synced_at", "TEXT"),
]

# Lookup enrichment fields. Same reasoning as above: an existing leads table
# will not gain them from CREATE TABLE IF NOT EXISTS.
_LEADS_ADDED_COLUMNS = [
    ("line_type",   "TEXT DEFAULT ''"),
    ("carrier",     "TEXT DEFAULT ''"),
    ("valid",       "INTEGER"),
    ("timezone",    "TEXT DEFAULT ''"),
    ("enriched_at", "TEXT"),
    ("last_called_at", "TEXT"),
]

# Same for recordings: databases created before media_url existed keep working.
_RECORDINGS_ADDED_COLUMNS = [
    ("media_url", "TEXT DEFAULT ''"),
]

# Message pricing, added with the cost report.
_SMS_LOG_ADDED_COLUMNS = [
    ("price",      "REAL"),
    ("price_unit", "TEXT DEFAULT ''"),
    # When the price was last asked for, so an unpriced row is retried later
    # instead of on every single sync.
    ("price_synced_at", "TEXT"),
]


def _add_missing_columns(conn, table, columns):
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    # Create the parent directory so DB_PATH can point at a mounted disk.
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(parent, exist_ok=True)

    conn = _configure(sqlite3.connect(DB_PATH, timeout=10))

    # Order matters: tables, then the column migration, then indexes. The new
    # indexes reference columns that an existing database does not have yet, so
    # creating them before the ALTER TABLE fails with "no such column".
    conn.executescript(SCHEMA)

    _add_missing_columns(conn, "call_log", _CALL_LOG_ADDED_COLUMNS)
    _add_missing_columns(conn, "leads", _LEADS_ADDED_COLUMNS)
    _add_missing_columns(conn, "recordings", _RECORDINGS_ADDED_COLUMNS)
    _add_missing_columns(conn, "sms_log", _SMS_LOG_ADDED_COLUMNS)

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


# ── Calling-window compliance ─────────────────────────────────────────────────

# NANP area code to IANA timezone. Only the zone matters, not the exact city, so
# one representative zone per area code is enough. Area codes that straddle a
# timezone boundary are mapped to the majority zone; that is a heuristic, and
# the window is deliberately conservative (see _local_hour).
AREA_CODE_TZ = {
    # Eastern
    "America/New_York": [
        "201","202","203","207","212","215","216","217","220","223","224","226","227",
        "229","231","234","239","240","242","246","248","249","252","260","264","267",
        "268","270","272","274","276","284","289","297","301","302","304","305","309",
        "312","313","315","317","321","326","330","331","332","340","341","345","347",
        "352","353","354","364","365","367","380","386","401","404","407","410","412",
        "413","416","419","423","434","437","438","440","441","443","445","447","448",
        "463","464","467","469","470","473","475","478","484","500","502","508","513",
        "514","516","517","518","519","540","548","551","557","561","567","570","571",
        "579","581","584","585","586","587","588","598","603","606","607","610","613",
        "614","615","616","617","618","631","636","646","647","649","656","658","667",
        "672","676","678","680","681","689","703","704","705","706","709","716","717",
        "718","724","727","732","734","740","743","754","757","758","762","763","765",
        "767","770","772","774","778","781","782","784","786","787","802","803","804",
        "807","809","810","812","813","814","815","816","819","828","829","839","840",
        "843","845","847","848","849","850","854","856","857","859","860","862","863",
        "864","865","867","869","870","872","873","876","878","879","900","901","902",
        "903","904","905","906","908","910","912","914","915","917","919","929","930",
        "931","934","936","937","938","939","941","945","947","948","954","956","959",
        "970","971","973","978","980","984","989",
    ],
    # Central
    "America/Chicago": [
        "205","210","214","218","224","225","228","251","254","256","262","281","309",
        "312","314","316","318","319","320","325","331","337","346","361","402","405",
        "409","414","417","430","432","445","469","479","501","502","504","507","512",
        "515","563","573","580","601","608","612","618","620","630","636","641","651",
        "660","662","682","708","713","715","731","737","763","769","773","779","785",
        "806","815","816","817","830","832","847","870","901","903","913","915","918",
        "920","936","940","952","956","972","979","985",
    ],
    # Mountain
    "America/Denver": [
        "303","307","308","385","406","435","505","575","719","720","801","915","970",
    ],
    "America/Phoenix": ["480", "520", "602", "623", "928"],
    # Pacific
    "America/Los_Angeles": [
        "206","209","213","236","250","253","279","310","323","341","350","360","408",
        "415","424","425","442","445","503","509","510","530","541","559","562","604",
        "619","626","628","650","657","661","669","671","672","702","707","714","725",
        "747","760","775","778","805","808","818","820","825","831","837","840","858",
        "907","909","916","925","949","951","971",
    ],
    "America/Anchorage": ["907"],
    "Pacific/Honolulu": ["808"],
}

# Built as area code -> zone. The dict above is authored zone-first because it
# reads better; the lookup needs the inverse. First zone listed wins for the
# handful of codes that appear twice.
_TZ_BY_AREA_CODE = {}
for _zone, _codes in AREA_CODE_TZ.items():
    for _code in _codes:
        _TZ_BY_AREA_CODE.setdefault(_code, _zone)


# Country calling code -> (country, IANA zone), for countries that sit in one
# timezone. Multi-zone countries are deliberately absent: +7 could be Kaliningrad
# or Kamchatka, nine hours apart, and a confidently wrong clock is worse than
# admitting the zone is unknown. NANP (+1) is resolved by area code above.
COUNTRY_TIMEZONES = {
    "20":  ("Egypt",          "Africa/Cairo"),
    "27":  ("South Africa",   "Africa/Johannesburg"),
    "30":  ("Greece",         "Europe/Athens"),
    "31":  ("Netherlands",    "Europe/Amsterdam"),
    "32":  ("Belgium",        "Europe/Brussels"),
    "33":  ("France",         "Europe/Paris"),
    "34":  ("Spain",          "Europe/Madrid"),
    "36":  ("Hungary",        "Europe/Budapest"),
    "39":  ("Italy",          "Europe/Rome"),
    "40":  ("Romania",        "Europe/Bucharest"),
    "41":  ("Switzerland",    "Europe/Zurich"),
    "43":  ("Austria",        "Europe/Vienna"),
    "44":  ("United Kingdom", "Europe/London"),
    "45":  ("Denmark",        "Europe/Copenhagen"),
    "46":  ("Sweden",         "Europe/Stockholm"),
    "47":  ("Norway",         "Europe/Oslo"),
    "48":  ("Poland",         "Europe/Warsaw"),
    "49":  ("Germany",        "Europe/Berlin"),
    "51":  ("Peru",           "America/Lima"),
    "52":  ("Mexico",         "America/Mexico_City"),
    "53":  ("Cuba",           "America/Havana"),
    "54":  ("Argentina",      "America/Argentina/Buenos_Aires"),
    "56":  ("Chile",          "America/Santiago"),
    "57":  ("Colombia",       "America/Bogota"),
    "58":  ("Venezuela",      "America/Caracas"),
    "60":  ("Malaysia",       "Asia/Kuala_Lumpur"),
    "62":  ("Indonesia",      "Asia/Jakarta"),
    "63":  ("Philippines",    "Asia/Manila"),
    "64":  ("New Zealand",    "Pacific/Auckland"),
    "65":  ("Singapore",      "Asia/Singapore"),
    "66":  ("Thailand",       "Asia/Bangkok"),
    "81":  ("Japan",          "Asia/Tokyo"),
    "82":  ("South Korea",    "Asia/Seoul"),
    "84":  ("Vietnam",        "Asia/Ho_Chi_Minh"),
    "86":  ("China",          "Asia/Shanghai"),
    "90":  ("Turkey",         "Europe/Istanbul"),
    "91":  ("India",          "Asia/Kolkata"),
    "92":  ("Pakistan",       "Asia/Karachi"),
    "94":  ("Sri Lanka",      "Asia/Colombo"),
    "95":  ("Myanmar",        "Asia/Yangon"),
    "98":  ("Iran",           "Asia/Tehran"),
    "212": ("Morocco",        "Africa/Casablanca"),
    "213": ("Algeria",        "Africa/Algiers"),
    "216": ("Tunisia",        "Africa/Tunis"),
    "234": ("Nigeria",        "Africa/Lagos"),
    "254": ("Kenya",          "Africa/Nairobi"),
    "255": ("Tanzania",       "Africa/Dar_es_Salaam"),
    "256": ("Uganda",         "Africa/Kampala"),
    "263": ("Zimbabwe",       "Africa/Harare"),
    "351": ("Portugal",       "Europe/Lisbon"),
    "352": ("Luxembourg",     "Europe/Luxembourg"),
    "353": ("Ireland",        "Europe/Dublin"),
    "354": ("Iceland",        "Atlantic/Reykjavik"),
    "356": ("Malta",          "Europe/Malta"),
    "357": ("Cyprus",         "Asia/Nicosia"),
    "358": ("Finland",        "Europe/Helsinki"),
    "359": ("Bulgaria",       "Europe/Sofia"),
    "370": ("Lithuania",      "Europe/Vilnius"),
    "371": ("Latvia",         "Europe/Riga"),
    "372": ("Estonia",        "Europe/Tallinn"),
    "374": ("Armenia",        "Asia/Yerevan"),
    "380": ("Ukraine",        "Europe/Kyiv"),
    "385": ("Croatia",        "Europe/Zagreb"),
    "386": ("Slovenia",       "Europe/Ljubljana"),
    "420": ("Czechia",        "Europe/Prague"),
    "421": ("Slovakia",       "Europe/Bratislava"),
    "886": ("Taiwan",         "Asia/Taipei"),
    "971": ("UAE",            "Asia/Dubai"),
    "972": ("Israel",         "Asia/Jerusalem"),
    "966": ("Saudi Arabia",   "Asia/Riyadh"),
    "974": ("Qatar",          "Asia/Qatar"),
    "977": ("Nepal",          "Asia/Kathmandu"),
    "852": ("Hong Kong",      "Asia/Hong_Kong"),
    "880": ("Bangladesh",     "Asia/Dhaka"),
}

# Country codes are one to three digits and not prefix-free (+1 vs +212), so a
# lookup has to try the longest first.
_COUNTRY_CODE_LENGTHS = (3, 2, 1)


def country_code_for_number(e164):
    """The calling code of an E.164 number, or None."""
    if not e164 or not e164.startswith("+"):
        return None
    digits = e164[1:]
    for size in _COUNTRY_CODE_LENGTHS:
        code = digits[:size]
        if code == "1" or code in COUNTRY_TIMEZONES:
            return code
    return None


def country_for_number(e164):
    """(country, IANA zone) for a number, either half possibly None.

    +1 is a special case: the country is known from the code but the zone needs
    the area code, and an unrecognised area code leaves the zone unknown.
    """
    code = country_code_for_number(e164)
    if code == "1":
        return "US/Canada", timezone_for_number(e164)
    if code in COUNTRY_TIMEZONES:
        return COUNTRY_TIMEZONES[code]
    return None, None


def timezone_for_number(e164):
    """Best-effort IANA timezone for a number, or None.

    NANP numbers resolve by area code; everything else by country calling code,
    for the countries that have exactly one zone. None means unknown, which
    callers treat as "allowed" rather than "out of window" — refusing to dial
    every unmapped country would be worse than the compliance risk it avoids.
    """
    if not e164 or not e164.startswith("+"):
        return None
    if e164.startswith("+1"):
        return _TZ_BY_AREA_CODE.get(e164[2:5]) if len(e164) == 12 else None

    digits = e164[1:]
    for size in _COUNTRY_CODE_LENGTHS:
        entry = COUNTRY_TIMEZONES.get(digits[:size])
        if entry:
            return entry[1]
    return None


def local_time_for_number(e164):
    """Current wall-clock time where the number is, or None if unknown."""
    zone = timezone_for_number(e164)
    if not zone or ZoneInfo is None:
        return None
    try:
        return datetime.now(ZoneInfo(zone))
    except Exception:
        return None


def call_window_status(e164):
    """Whether it is an acceptable local hour to call this number.

    Returns (allowed, reason). An unknown timezone is allowed — see
    timezone_for_number — but the reason says so, and the UI shows it.
    """
    if not QUIET_HOURS_ENFORCED:
        return True, "quiet hours not enforced"

    local = local_time_for_number(e164)
    if local is None:
        return True, "timezone unknown"

    hour = local.hour
    if CALL_WINDOW_START <= hour < CALL_WINDOW_END:
        return True, f"{local:%H:%M} local"
    return False, (
        f"{local:%H:%M} local — outside the "
        f"{CALL_WINDOW_START:02d}:00–{CALL_WINDOW_END:02d}:00 calling window"
    )


# ── Lead activity timeline ────────────────────────────────────────────────────

def log_activity(db, lead_id, kind, body="", ref=""):
    """Append one timeline entry. Never raises — a failed log must not fail a call."""
    if not lead_id:
        return
    try:
        db.execute(
            """INSERT INTO activities (lead_id, kind, body, ref, owner_id)
               VALUES (?, ?, ?, ?, ?)""",
            (lead_id, kind, body, ref, DEFAULT_OWNER),
        )
    except Exception:
        app.logger.exception("activity log failed")


def lead_id_for_number(db, e164):
    if not e164:
        return None
    row = db.execute(
        "SELECT id FROM leads WHERE phone = ? AND owner_id = ?", (e164, DEFAULT_OWNER)
    ).fetchone()
    return row["id"] if row else None


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

    # Calling outside the callee's local window is a TCPA violation, so it is
    # refused here as well as in the UI — the UI check is advisory, this one is
    # the one Twilio actually obeys.
    allowed, reason = call_window_status(to_number)
    if not allowed:
        app.logger.warning("Blocked out-of-window call to %s: %s", to_number, reason)
        response.say("This number cannot be called at this hour.")
        return str(response), 200, {"Content-Type": "text/xml"}

    # Consent announcement has to play before the parties are bridged, so it goes
    # on the parent call rather than inside <Dial>.
    if RECORD_CALLS and RECORDING_ANNOUNCEMENT:
        response.say(RECORDING_ANNOUNCEMENT)

    dial_kwargs = {
        "caller_id": TWILIO_PHONE_NUMBER,
        "answer_on_bridge": True,
        "timeout": 20,
    }
    rec_cb = _status_callback_url("/recording_status")
    if RECORD_CALLS:
        dial_kwargs["record"] = RECORDING_MODE
        if rec_cb:
            dial_kwargs["recording_status_callback"] = rec_cb
            dial_kwargs["recording_status_callback_event"] = "completed"
            dial_kwargs["recording_status_callback_method"] = "POST"

    dial = Dial(**dial_kwargs)

    number_kwargs = {}
    cb = _status_callback_url("/call_status")
    if cb:
        number_kwargs.update(
            status_callback=cb,
            status_callback_event="initiated ringing answered completed",
            status_callback_method="POST",
        )

    # AMD only reports; it does not change routing on its own. /amd_status
    # records the verdict so a machine-answered call can be dispositioned
    # automatically instead of the agent guessing.
    amd_cb = _status_callback_url("/amd_status")
    if AMD_ENABLED and amd_cb:
        number_kwargs.update(
            machine_detection=AMD_MODE,
            amd_status_callback=amd_cb,
            amd_status_callback_method="POST",
        )

    dial.number(to_number, **number_kwargs)
    response.append(dial)
    return str(response), 200, {"Content-Type": "text/xml"}


@app.route("/voicemail_drop", methods=["GET", "POST"])
@twilio_webhook
def voicemail_drop():
    """TwiML that leaves a pre-recorded message, then hangs up.

    Reached when the agent hits "drop voicemail" during a call that AMD flagged
    as a machine: the app redirects the child leg here, so the agent is freed
    immediately while Twilio plays the message.
    """
    response = VoiceResponse()
    if VOICEMAIL_AUDIO_URL:
        response.play(VOICEMAIL_AUDIO_URL)
    elif VOICEMAIL_MESSAGE:
        response.say(VOICEMAIL_MESSAGE)
    else:
        response.say("Sorry we missed you. We will try again later.")
    response.hangup()
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
    _touch_lead_after_call(call_sid, request.form.get("CallStatus", ""))
    return ("", 204)


def _touch_lead_after_call(call_sid, status):
    """Stamp the lead's last_called_at and drop a timeline entry once a call ends.

    Only on a terminal status: 'initiated' and 'ringing' fire for every attempt
    and would make the timeline three entries deep per dial.
    """
    if status not in TERMINAL_STATUSES:
        return

    db  = get_db()
    row = db.execute(
        """SELECT lead_id, to_number, duration, answered_by
           FROM call_log WHERE call_sid = ?""",
        (call_sid,),
    ).fetchone()
    if not row:
        return

    lead_id = row["lead_id"] or lead_id_for_number(db, row["to_number"])
    if not lead_id:
        return

    db.execute(
        """UPDATE leads SET last_called_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (lead_id,),
    )
    db.commit()


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


# ── Answering machine detection ───────────────────────────────────────────────

# AnsweredBy values Twilio reports. Anything in MACHINE_ANSWERS means a human
# never picked up, so the call should not count as a connect.
MACHINE_ANSWERS = ("machine_start", "machine_end_beep", "machine_end_silence",
                   "machine_end_other", "fax")


@app.route("/amd_status", methods=["POST"])
@twilio_webhook
def amd_status():
    """Record AMD's verdict and auto-disposition machine answers.

    The callback fires on the child leg, whose CallSid differs from the parent
    the browser knows about, so the row is matched on either.
    """
    call_sid    = request.form.get("CallSid", "")
    answered_by = request.form.get("AnsweredBy", "")
    if not call_sid:
        return ("", 204)

    db = get_db()
    db.execute(
        """INSERT INTO call_log (call_sid, answered_by, owner_id)
           VALUES (?, ?, ?)
           ON CONFLICT(call_sid) DO UPDATE SET
               answered_by = excluded.answered_by,
               updated_at  = CURRENT_TIMESTAMP""",
        (call_sid, answered_by, DEFAULT_OWNER),
    )
    # Propagate to the parent leg so the browser, which only knows the parent
    # SID, can see the verdict too.
    parent = request.form.get("ParentCallSid", "")
    if parent:
        db.execute(
            "UPDATE call_log SET answered_by = ? WHERE call_sid = ?",
            (answered_by, parent),
        )

    if answered_by in MACHINE_ANSWERS:
        row = db.execute(
            "SELECT lead_id, to_number FROM call_log WHERE call_sid = ?", (call_sid,)
        ).fetchone()
        lead_id = row["lead_id"] if row else None
        if not lead_id and row:
            lead_id = lead_id_for_number(db, row["to_number"])
        log_activity(db, lead_id, "amd", f"Answering machine detected ({answered_by})",
                     call_sid)

    db.commit()
    return ("", 204)


# ── Call recording ────────────────────────────────────────────────────────────

@app.route("/recording_status", methods=["POST"])
@twilio_webhook
def recording_status():
    recording_sid = request.form.get("RecordingSid", "")
    call_sid      = request.form.get("CallSid", "")
    if not recording_sid:
        return ("", 204)

    duration = request.form.get("RecordingDuration") or ""
    channels = request.form.get("RecordingChannels") or "1"

    db      = get_db()
    row     = db.execute(
        "SELECT lead_id, to_number FROM call_log WHERE call_sid = ?", (call_sid,)
    ).fetchone()
    lead_id = row["lead_id"] if row else None
    if not lead_id and row:
        lead_id = lead_id_for_number(db, row["to_number"])

    db.execute(
        """INSERT INTO recordings (recording_sid, call_sid, lead_id, duration,
                                   channels, status, media_url, owner_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(recording_sid) DO UPDATE SET
               status    = excluded.status,
               duration  = excluded.duration,
               media_url = COALESCE(NULLIF(excluded.media_url, ''), recordings.media_url)""",
        (recording_sid, call_sid, lead_id,
         int(duration) if duration.isdigit() else None,
         int(channels) if channels.isdigit() else 1,
         request.form.get("RecordingStatus", "completed"),
         (request.form.get("RecordingUrl") or "").strip(), DEFAULT_OWNER),
    )
    db.execute(
        "UPDATE call_log SET recording_sid = ? WHERE call_sid = ?",
        (recording_sid, call_sid),
    )
    log_activity(db, lead_id, "recording", f"Recording available ({duration}s)",
                 recording_sid)
    db.commit()
    return ("", 204)


@app.route("/api/config", methods=["GET"])
def client_config():
    """Feature flags the UI needs so it can hide controls that would 400.

    No secrets here — only whether an optional capability is switched on.
    """
    return jsonify({
        "recording":       RECORD_CALLS,
        "amd":             AMD_ENABLED,
        "voicemail_drop":  bool(VOICEMAIL_AUDIO_URL or VOICEMAIL_MESSAGE),
        "quiet_hours":     QUIET_HOURS_ENFORCED,
        "call_window":     [CALL_WINDOW_START, CALL_WINDOW_END],
        "messaging_service": bool(TWILIO_MESSAGING_SERVICE_SID),
        "public_base_url": bool(PUBLIC_BASE_URL),
        "merge_fields":    list(MERGE_FIELDS),
        "sms_max_length":  SMS_MAX_LENGTH,
    })


@app.route("/api/calls/<call_sid>/voicemail-drop", methods=["POST"])
def drop_voicemail(call_sid):
    """Hand the live call off to the voicemail TwiML and free the agent.

    Redirects the child (callee) leg, not the parent: redirecting the parent
    would play the message to the agent's own browser.
    """
    if not (VOICEMAIL_AUDIO_URL or VOICEMAIL_MESSAGE):
        return jsonify({"error": "no voicemail message configured"}), 400
    if not PUBLIC_BASE_URL:
        return jsonify({"error": "PUBLIC_BASE_URL must be set to drop voicemail"}), 400

    try:
        client = get_client()
        # The browser only knows the parent SID. The leg that is actually on the
        # phone with the machine is its child.
        children = list(client.calls.list(parent_call_sid=call_sid, limit=5))
        target   = children[0].sid if children else call_sid
        client.calls(target).update(url=PUBLIC_BASE_URL + "/voicemail_drop",
                                    method="POST")
    except Exception as e:
        app.logger.exception("voicemail drop failed")
        return jsonify({"error": str(e)}), 502

    db      = get_db()
    row     = db.execute("SELECT lead_id, to_number FROM call_log WHERE call_sid = ?",
                         (call_sid,)).fetchone()
    lead_id = (row["lead_id"] if row else None) or (
        lead_id_for_number(db, row["to_number"]) if row else None)
    log_activity(db, lead_id, "voicemail", "Voicemail dropped", call_sid)
    db.commit()
    return jsonify({"ok": True, "call_sid": target})


@app.route("/api/recordings", methods=["GET"])
def list_recordings():
    limit = min(int(request.args.get("limit", 50)), 200)
    rows  = get_db().execute(
        """SELECT r.recording_sid, r.call_sid, r.lead_id, r.duration, r.channels,
                  r.created_at, l.name, l.phone
           FROM recordings r
           LEFT JOIN leads l ON l.id = r.lead_id
           WHERE r.owner_id = ?
           ORDER BY r.created_at DESC LIMIT ?""",
        (DEFAULT_OWNER, limit),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/recordings/<recording_sid>/audio", methods=["GET"])
def recording_audio(recording_sid):
    """Stream a recording through the app rather than exposing Twilio credentials.

    Twilio's media URL requires HTTP basic auth with the account SID and auth
    token. Handing that URL to the browser would either leak the token or 401,
    so the audio is proxied.
    """
    if not re.fullmatch(r"RE[0-9a-fA-F]{32}", recording_sid):
        return jsonify({"error": "invalid recording sid"}), 400

    row = get_db().execute(
        "SELECT media_url FROM recordings WHERE recording_sid = ? AND owner_id = ?",
        (recording_sid, DEFAULT_OWNER),
    ).fetchone()

    # The callback's own URL already carries the right account SID; rebuilding
    # it from TWILIO_ACCOUNT_SID is only a fallback for rows recorded before
    # media_url existed.
    base = (row["media_url"] if row and row["media_url"] else
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}"
            f"/Recordings/{recording_sid}")
    base = re.sub(r"\.(mp3|wav)$", "", base)

    # A dual-channel recording can take a few seconds after the 'completed'
    # callback before the mp3 transcode exists; the wav is there immediately.
    # Trying both turns a spurious 404 into playable audio.
    upstream = None
    for suffix, content_type in ((".mp3", "audio/mpeg"), (".wav", "audio/wav")):
        try:
            resp = requests.get(
                base + suffix,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
                headers={k: v for k, v in (("Range", request.headers.get("Range")),) if v},
                stream=True,
                timeout=30,
            )
        except requests.RequestException as e:
            app.logger.exception("recording fetch failed")
            return jsonify({"error": str(e)}), 502
        if resp.status_code in (200, 206):
            upstream = (resp, content_type)
            break
        resp.close()
        last_status = resp.status_code

    if upstream is None:
        app.logger.warning("recording %s unavailable upstream (%s)",
                           recording_sid, last_status)
        if last_status == 404:
            return jsonify({
                "error": "recording not found on Twilio — it may still be "
                         "processing, or it was deleted"
            }), 404
        return jsonify({"error": "recording not available"}), last_status

    resp, content_type = upstream
    headers = {"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"}
    for h in ("Content-Range", "Content-Length"):
        if h in resp.headers:
            headers[h] = resp.headers[h]

    return Response(
        resp.iter_content(chunk_size=8192),
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", content_type),
        headers=headers,
    )


@app.route("/api/recordings/<recording_sid>", methods=["DELETE"])
def delete_recording(recording_sid):
    """Delete on Twilio as well as locally. A local-only delete keeps billing
    and the actual audio alive, which is the opposite of what deleting means."""
    if not re.fullmatch(r"RE[0-9a-fA-F]{32}", recording_sid):
        return jsonify({"error": "invalid recording sid"}), 400
    try:
        get_client().recordings(recording_sid).delete()
    except Exception as e:
        app.logger.warning("Twilio recording delete failed: %s", e)

    db = get_db()
    db.execute("DELETE FROM recordings WHERE recording_sid = ?", (recording_sid,))
    db.execute("UPDATE call_log SET recording_sid = '' WHERE recording_sid = ?",
               (recording_sid,))
    db.commit()
    return ("", 204)


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

def sms_too_long(body):
    """The error payload for an over-length message, or None if it fits."""
    if len(body) <= SMS_MAX_LENGTH:
        return None
    return {
        "error": f"message is {len(body)} characters; the limit is "
                 f"{SMS_MAX_LENGTH} so it stays a single SMS segment",
        "length": len(body),
        "max_length": SMS_MAX_LENGTH,
    }


def _message_kwargs(to, body):
    """Shared arguments for messages.create, including the delivery callback.

    Twilio reports 'queued' at send time and nothing else unless a status
    callback is registered, which is why delivered/failed counts need this.
    """
    kwargs = {"to": to, "body": body}
    if TWILIO_MESSAGING_SERVICE_SID:
        kwargs["messaging_service_sid"] = TWILIO_MESSAGING_SERVICE_SID
    else:
        kwargs["from_"] = TWILIO_PHONE_NUMBER
    cb = _status_callback_url("/sms_status")
    if cb:
        kwargs["status_callback"] = cb
    return kwargs


@app.route("/send_sms", methods=["POST"])
def send_sms():
    data = request.get_json(silent=True) or {}
    to   = normalize_number(data.get("to", ""))
    body = (data.get("body") or "").strip()

    if not to:
        return jsonify({"error": "invalid destination number"}), 400
    if not body:
        return jsonify({"error": "message body is empty"}), 400
    over = sms_too_long(body)
    if over:
        return jsonify(over), 400
    if not is_dialable(to):
        return jsonify({"error": "destination not permitted"}), 403

    try:
        client = get_client()
        # Prefer the Messaging Service when configured: it carries the A2P 10DLC
        # campaign registration that US carriers require, and handles routing.
        msg = client.messages.create(**_message_kwargs(to, body))

        db      = get_db()
        lead_id = lead_id_for_number(db, to)
        _log_sms(db, msg.sid, lead_id, "outbound", TWILIO_PHONE_NUMBER, to, body,
                 msg.status)
        db.commit()
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
        "line_type":      _get(r, "line_type") or "",
        "carrier":        _get(r, "carrier") or "",
        "valid":          _as_bool(_get(r, "valid")),
        "timezone":       _get(r, "timezone") or timezone_for_number(r["phone"]) or "",
        "enriched_at":    _get(r, "enriched_at") or "",
        "last_called_at": _get(r, "last_called_at") or "",
    }


def _get(row, key):
    """Read a column that a row from an un-migrated table may not carry."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _as_bool(val):
    """Tri-state: None stays None so 'never checked' differs from 'invalid'."""
    return None if val is None else bool(val)


@app.route("/api/leads", methods=["GET"])
def list_leads():
    q         = (request.args.get("q") or "").strip()
    status    = (request.args.get("status") or "").strip()
    tag       = (request.args.get("tag") or "").strip()
    line_type = (request.args.get("line_type") or "").strip()
    dnc       = (request.args.get("dnc") or "").strip()
    uncalled  = (request.args.get("uncalled") or "").strip()
    sort      = (request.args.get("sort") or "recent").strip()
    limit     = min(int(request.args.get("limit", 200)), 1000)
    offset    = int(request.args.get("offset", 0))

    sql    = "SELECT * FROM leads WHERE owner_id = ?"
    params = [DEFAULT_OWNER]

    if q:
        sql += " AND (name LIKE ? OR phone LIKE ? OR company LIKE ? OR email LIKE ?)"
        params += [f"%{q}%"] * 4
    if status:
        sql += " AND status = ?"
        params.append(status)
    if tag:
        # tags is a comma-joined string; match on the whole token so 'vip' does
        # not also match 'vip-churned'.
        sql += " AND (',' || REPLACE(tags, ', ', ',') || ',') LIKE ?"
        params.append(f"%,{tag},%")
    if line_type:
        sql += " AND line_type = ?"
        params.append(line_type)
    if dnc in ("0", "1"):
        sql += " AND dnc = ?"
        params.append(int(dnc))
    if uncalled in ("1", "true"):
        sql += " AND (last_called_at IS NULL OR last_called_at = '')"

    ORDERINGS = {
        "recent":  "updated_at DESC",
        "created": "created_at DESC",
        "name":    "name COLLATE NOCASE ASC",
        "company": "company COLLATE NOCASE ASC",
        "called":  "last_called_at DESC",
        "oldest":  "created_at ASC",
    }
    sql += f" ORDER BY {ORDERINGS.get(sort, ORDERINGS['recent'])} LIMIT ? OFFSET ?"
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
        """SELECT call_sid, status, disposition, note, duration, updated_at,
                  answered_by, recording_sid
           FROM call_log WHERE lead_id = ? ORDER BY updated_at DESC LIMIT 50""",
        (lead_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ══════════════════════════════════════════════════════════════════════════════
# Lead timeline — calls, texts, notes and events in one chronological list
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/leads/<int:lead_id>/timeline", methods=["GET"])
def lead_timeline(lead_id):
    db    = get_db()
    limit = min(int(request.args.get("limit", 100)), 300)
    items = []

    for r in db.execute(
        """SELECT call_sid, status, disposition, note, duration, direction,
                  answered_by, recording_sid, updated_at
           FROM call_log WHERE lead_id = ? ORDER BY updated_at DESC LIMIT ?""",
        (lead_id, limit),
    ):
        items.append({
            "kind":          "call",
            "at":            r["updated_at"],
            "ref":           r["call_sid"],
            "status":        r["status"],
            "disposition":   r["disposition"] or "",
            "note":          r["note"] or "",
            "duration":      r["duration"],
            "direction":     r["direction"] or "",
            "answered_by":   r["answered_by"] or "",
            "recording_sid": r["recording_sid"] or "",
        })

    for r in db.execute(
        """SELECT message_sid, direction, body, created_at
           FROM sms_log WHERE lead_id = ? ORDER BY created_at DESC LIMIT ?""",
        (lead_id, limit),
    ):
        items.append({
            "kind":      "sms",
            "at":        r["created_at"],
            "ref":       r["message_sid"],
            "direction": r["direction"],
            "body":      r["body"],
        })

    for r in db.execute(
        """SELECT kind, body, ref, created_at FROM activities
           WHERE lead_id = ? ORDER BY created_at DESC LIMIT ?""",
        (lead_id, limit),
    ):
        items.append({
            "kind": r["kind"], "at": r["created_at"],
            "body": r["body"], "ref": r["ref"],
        })

    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return jsonify(items[:limit])


@app.route("/api/leads/<int:lead_id>/notes", methods=["POST"])
def add_lead_note(lead_id):
    body = (request.get_json(silent=True) or {}).get("body", "").strip()
    if not body:
        return jsonify({"error": "note body is empty"}), 400

    db = get_db()
    if not db.execute("SELECT 1 FROM leads WHERE id = ? AND owner_id = ?",
                      (lead_id, DEFAULT_OWNER)).fetchone():
        return jsonify({"error": "not found"}), 404

    log_activity(db, lead_id, "note", body)
    db.execute("UPDATE leads SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (lead_id,))
    db.commit()
    return jsonify({"ok": True}), 201


# ══════════════════════════════════════════════════════════════════════════════
# Lookup enrichment — carrier, line type, and whether the number exists at all
# ══════════════════════════════════════════════════════════════════════════════

LOOKUP_URL = "https://lookups.twilio.com/v2/PhoneNumbers/{}"


def lookup_number(e164):
    """Fetch line-type intelligence for one number.

    Returns a dict with valid/line_type/carrier, or raises. Lookup is billed per
    request, so callers should persist the result rather than re-query.
    """
    resp = requests.get(
        LOOKUP_URL.format(e164),
        params={"Fields": "line_type_intelligence"},
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=15,
    )
    if resp.status_code == 404:
        # Lookup returns 404 for a number that does not exist in any carrier
        # database — that is a definitive "invalid", not a transport failure.
        return {"valid": False, "line_type": "", "carrier": ""}
    resp.raise_for_status()

    data = resp.json()
    lti  = data.get("line_type_intelligence") or {}
    return {
        "valid":     bool(data.get("valid")),
        "line_type": lti.get("type") or "",
        "carrier":   lti.get("carrier_name") or "",
    }


def _apply_enrichment(db, lead_id, phone, info):
    db.execute(
        """UPDATE leads SET line_type = ?, carrier = ?, valid = ?, timezone = ?,
                            enriched_at = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND owner_id = ?""",
        (info["line_type"], info["carrier"], int(info["valid"]),
         timezone_for_number(phone) or "", lead_id, DEFAULT_OWNER),
    )
    label = info["line_type"] or "unknown line type"
    if info["carrier"]:
        label += f" · {info['carrier']}"
    if not info["valid"]:
        label = "number not in service · " + label
    log_activity(db, lead_id, "enrich", label)


@app.route("/api/leads/<int:lead_id>/enrich", methods=["POST"])
def enrich_lead(lead_id):
    db  = get_db()
    row = db.execute("SELECT id, phone FROM leads WHERE id = ? AND owner_id = ?",
                     (lead_id, DEFAULT_OWNER)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404

    try:
        info = lookup_number(row["phone"])
    except Exception as e:
        app.logger.exception("lookup failed")
        return jsonify({"error": str(e)}), 502

    _apply_enrichment(db, lead_id, row["phone"], info)
    db.commit()

    lead = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return jsonify(_lead_row(lead))


@app.route("/api/leads/enrich", methods=["POST"])
def enrich_leads_bulk():
    """Enrich up to `limit` leads that have never been looked up.

    Bounded on purpose: Lookup is billed per number, and an unbounded sweep over
    a freshly imported 50k-row list is a surprise invoice.
    """
    data  = request.get_json(silent=True) or {}
    limit = min(int(data.get("limit", 50)), 200)
    force = bool(data.get("force"))

    sql = "SELECT id, phone FROM leads WHERE owner_id = ?"
    if not force:
        sql += " AND (enriched_at IS NULL OR enriched_at = '')"
    sql += " ORDER BY created_at DESC LIMIT ?"

    db   = get_db()
    rows = db.execute(sql, (DEFAULT_OWNER, limit)).fetchall()

    enriched, failed, invalid = 0, 0, 0
    for row in rows:
        try:
            info = lookup_number(row["phone"])
        except Exception:
            failed += 1
            continue
        _apply_enrichment(db, row["id"], row["phone"], info)
        enriched += 1
        if not info["valid"]:
            invalid += 1
    db.commit()

    return jsonify({"enriched": enriched, "failed": failed, "invalid": invalid,
                    "remaining_checked": len(rows)})


# ══════════════════════════════════════════════════════════════════════════════
# Calling-window compliance
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/compliance/check", methods=["GET"])
def compliance_check():
    """Whether a number may be dialed right now, and why not if it may not.

    The UI calls this before enabling the dial button so the agent gets a reason
    up front instead of a spoken refusal after Twilio has already been billed.
    """
    number = normalize_number(request.args.get("number", ""))
    if not number:
        return jsonify({"error": "invalid number"}), 400

    allowed, reason = call_window_status(number)
    local           = local_time_for_number(number)
    dnc_row         = get_db().execute(
        "SELECT dnc FROM leads WHERE phone = ? AND owner_id = ?",
        (number, DEFAULT_OWNER),
    ).fetchone()
    on_dnc = bool(dnc_row and dnc_row["dnc"])

    country, _zone = country_for_number(number)

    return jsonify({
        "number":       number,
        "dialable":     is_dialable(number) and allowed and not on_dnc,
        "dnc":          on_dnc,
        "in_window":    allowed,
        "reason":       "on the do-not-call list" if on_dnc else reason,
        "timezone":     timezone_for_number(number) or "",
        "local_time":   local.isoformat() if local else "",
        # The dialer shows a live clock for the callee, which needs the zone to
        # tick locally and the country to label it.
        "country":      country or "",
        "country_code": country_code_for_number(number) or "",
        "enforced":     QUIET_HOURS_ENFORCED,
        "window":       [CALL_WINDOW_START, CALL_WINDOW_END],
    })


# ══════════════════════════════════════════════════════════════════════════════
# Follow-up tasks
# ══════════════════════════════════════════════════════════════════════════════

def _parse_due(raw):
    """Accept an ISO timestamp or a '+N<unit>' shorthand, return UTC ISO.

    The shorthand exists because the common case during a call is "call back in
    2 hours", and making the agent compute a timestamp for that is friction.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    rel = re.fullmatch(r"\+(\d+)\s*([mhdw])", raw.lower())
    if rel:
        n    = int(rel.group(1))
        unit = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[rel.group(2)]
        due  = datetime.now(timezone.utc) + timedelta(**{unit: n})
        return due.strftime("%Y-%m-%d %H:%M:%S")

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _task_row(r):
    return {
        "id":      r["id"],
        "lead_id": r["lead_id"],
        "title":   r["title"],
        "due_at":  r["due_at"],
        "done":    bool(r["done"]),
        "name":    _get(r, "name") or "",
        "phone":   _get(r, "phone") or "",
        "overdue": (not r["done"]) and (r["due_at"] or "") <=
                   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    scope = (request.args.get("scope") or "open").strip()
    limit = min(int(request.args.get("limit", 200)), 500)

    sql    = """SELECT t.*, l.name, l.phone FROM tasks t
                LEFT JOIN leads l ON l.id = t.lead_id
                WHERE t.owner_id = ?"""
    params = [DEFAULT_OWNER]

    if scope == "open":
        sql += " AND t.done = 0"
    elif scope == "done":
        sql += " AND t.done = 1"
    elif scope == "overdue":
        sql += " AND t.done = 0 AND t.due_at <= ?"
        params.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    elif scope == "today":
        sql += " AND t.done = 0 AND date(t.due_at) <= date('now')"

    if request.args.get("lead_id"):
        sql += " AND t.lead_id = ?"
        params.append(int(request.args["lead_id"]))

    sql += " ORDER BY t.done ASC, t.due_at ASC LIMIT ?"
    params.append(limit)

    rows = get_db().execute(sql, params).fetchall()
    return jsonify([_task_row(r) for r in rows])


@app.route("/api/tasks", methods=["POST"])
def create_task():
    data  = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    due   = _parse_due(data.get("due_at") or "+1d")

    if not title:
        return jsonify({"error": "title required"}), 400
    if not due:
        return jsonify({"error": "due_at must be ISO8601 or a +2h / +3d shorthand"}), 400

    db  = get_db()
    cur = db.execute(
        "INSERT INTO tasks (lead_id, title, due_at, owner_id) VALUES (?, ?, ?, ?)",
        (data.get("lead_id"), title, due, DEFAULT_OWNER),
    )
    log_activity(db, data.get("lead_id"), "task", f"Follow-up scheduled: {title} ({due} UTC)")
    db.commit()

    row = db.execute(
        """SELECT t.*, l.name, l.phone FROM tasks t
           LEFT JOIN leads l ON l.id = t.lead_id WHERE t.id = ?""",
        (cur.lastrowid,),
    ).fetchone()
    return jsonify(_task_row(row)), 201


@app.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def update_task(task_id):
    data  = request.get_json(silent=True) or {}
    sets, params = [], []

    if "done" in data:
        sets.append("done = ?")
        params.append(int(bool(data["done"])))
    if "title" in data:
        sets.append("title = ?")
        params.append((data["title"] or "").strip())
    if "due_at" in data:
        due = _parse_due(data["due_at"])
        if not due:
            return jsonify({"error": "invalid due_at"}), 400
        sets.append("due_at = ?")
        params.append(due)

    if not sets:
        return jsonify({"error": "nothing to update"}), 400

    sets.append("updated_at = CURRENT_TIMESTAMP")
    params += [task_id, DEFAULT_OWNER]

    db = get_db()
    db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ? AND owner_id = ?", params)
    db.commit()

    row = db.execute(
        """SELECT t.*, l.name, l.phone FROM tasks t
           LEFT JOIN leads l ON l.id = t.lead_id WHERE t.id = ?""",
        (task_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(_task_row(row))


@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    db = get_db()
    db.execute("DELETE FROM tasks WHERE id = ? AND owner_id = ?", (task_id, DEFAULT_OWNER))
    db.commit()
    return ("", 204)


# ══════════════════════════════════════════════════════════════════════════════
# SMS templates and bulk send
# ══════════════════════════════════════════════════════════════════════════════

MERGE_FIELDS = ("name", "first_name", "company", "phone", "email")


def render_merge(body, lead):
    """Substitute {{field}} placeholders from a lead row.

    Unknown placeholders are left as-is rather than blanked, so a typo is
    visible in the preview instead of silently sending a sentence with a hole.
    """
    def sub(match):
        key = match.group(1).strip().lower()
        if key not in MERGE_FIELDS:
            return match.group(0)
        if key == "first_name":
            return ((lead["name"] or "").strip().split(" ") or [""])[0]
        return str(lead[key] if key in lead.keys() else "") or ""

    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", sub, body or "", flags=re.I)


@app.route("/api/templates", methods=["GET"])
def list_templates():
    rows = get_db().execute(
        "SELECT * FROM templates WHERE owner_id = ? ORDER BY name COLLATE NOCASE",
        (DEFAULT_OWNER,),
    ).fetchall()
    return jsonify([
        {"id": r["id"], "name": r["name"], "body": r["body"]} for r in rows
    ])


@app.route("/api/templates", methods=["POST"])
def create_template():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    body = (data.get("body") or "").strip()
    if not name or not body:
        return jsonify({"error": "name and body required"}), 400
    over = sms_too_long(body)
    if over:
        return jsonify(over), 400

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO templates (name, body, owner_id) VALUES (?, ?, ?)",
            (name, body, DEFAULT_OWNER),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "a template with that name already exists"}), 409
    return jsonify({"id": cur.lastrowid, "name": name, "body": body}), 201


@app.route("/api/templates/<int:tpl_id>", methods=["PATCH", "DELETE"])
def modify_template(tpl_id):
    db = get_db()
    if request.method == "DELETE":
        db.execute("DELETE FROM templates WHERE id = ? AND owner_id = ?",
                   (tpl_id, DEFAULT_OWNER))
        db.commit()
        return ("", 204)

    data = request.get_json(silent=True) or {}
    if "body" in data:
        over = sms_too_long((data.get("body") or "").strip())
        if over:
            return jsonify(over), 400

    sets, params = [], []
    for field in ("name", "body"):
        if field in data:
            sets.append(f"{field} = ?")
            params.append((data[field] or "").strip())
    if not sets:
        return jsonify({"error": "nothing to update"}), 400

    sets.append("updated_at = CURRENT_TIMESTAMP")
    params += [tpl_id, DEFAULT_OWNER]
    db.execute(f"UPDATE templates SET {', '.join(sets)} WHERE id = ? AND owner_id = ?",
               params)
    db.commit()

    row = db.execute("SELECT * FROM templates WHERE id = ?", (tpl_id,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({"id": row["id"], "name": row["name"], "body": row["body"]})


@app.route("/api/sms/bulk", methods=["POST"])
def bulk_sms():
    """Send one templated message to many leads.

    Do-not-call leads are skipped without being counted as failures — that is
    the whole point of the flag, and reporting them as errors would train the
    user to ignore the error list.
    """
    data     = request.get_json(silent=True) or {}
    body     = (data.get("body") or "").strip()
    lead_ids = data.get("lead_ids") or []
    tpl_id   = data.get("template_id")
    limit    = min(int(data.get("limit", 200)), 500)

    db = get_db()
    if tpl_id and not body:
        tpl = db.execute("SELECT body FROM templates WHERE id = ? AND owner_id = ?",
                         (tpl_id, DEFAULT_OWNER)).fetchone()
        if not tpl:
            return jsonify({"error": "template not found"}), 404
        body = tpl["body"]

    if not body:
        return jsonify({"error": "message body is empty"}), 400
    if not lead_ids:
        return jsonify({"error": "lead_ids required"}), 400
    # The raw template is checked here; merge fields can still push an individual
    # rendered message over, which is caught per lead below.
    over = sms_too_long(body)
    if over:
        return jsonify(over), 400

    lead_ids = [int(i) for i in lead_ids][:limit]
    marks    = ",".join("?" * len(lead_ids))
    rows     = db.execute(
        f"SELECT * FROM leads WHERE owner_id = ? AND id IN ({marks})",
        (DEFAULT_OWNER, *lead_ids),
    ).fetchall()

    client = get_client()
    sent, skipped, errors = 0, 0, []

    for lead in rows:
        if lead["dnc"]:
            skipped += 1
            continue
        if not is_dialable(lead["phone"]):
            errors.append({"lead_id": lead["id"], "error": "destination not permitted"})
            continue

        text = render_merge(body, lead)
        # A long company or name can push a template that fits over the limit.
        # Skipping the one lead beats splitting the text or failing the batch.
        over = sms_too_long(text)
        if over:
            errors.append({"lead_id": lead["id"], "error": over["error"]})
            continue

        try:
            msg = client.messages.create(**_message_kwargs(lead["phone"], text))
        except Exception as e:
            errors.append({"lead_id": lead["id"], "error": str(e)})
            continue

        _log_sms(db, msg.sid, lead["id"], "outbound", TWILIO_PHONE_NUMBER,
                 lead["phone"], text, msg.status)
        sent += 1

    db.commit()
    return jsonify({"sent": sent, "skipped_dnc": skipped, "errors": errors})


@app.route("/api/templates/preview", methods=["POST"])
def preview_template():
    data = request.get_json(silent=True) or {}
    body = data.get("body") or ""
    lead = get_db().execute(
        "SELECT * FROM leads WHERE id = ? AND owner_id = ?",
        (data.get("lead_id"), DEFAULT_OWNER),
    ).fetchone()
    if not lead:
        return jsonify({"error": "lead not found"}), 404
    rendered = render_merge(body, lead)
    return jsonify({
        "body": rendered,
        "length": len(rendered),
        "max_length": SMS_MAX_LENGTH,
        "too_long": len(rendered) > SMS_MAX_LENGTH,
    })


# ══════════════════════════════════════════════════════════════════════════════
# Inbound SMS
# ══════════════════════════════════════════════════════════════════════════════

def _log_sms(db, sid, lead_id, direction, from_number, to_number, body, status=""):
    db.execute(
        """INSERT INTO sms_log (message_sid, lead_id, direction, from_number,
                                to_number, body, status, owner_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(message_sid) DO UPDATE SET status = excluded.status""",
        (sid, lead_id, direction, from_number, to_number, body, status, DEFAULT_OWNER),
    )


# Replies that must stop all further contact. Carriers already handle STOP for
# A2P traffic, but the CRM has to reflect it or the lead stays in campaigns.
OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit",
                    "remove", "optout", "opt-out"}


@app.route("/sms_status", methods=["POST"])
@twilio_webhook
def sms_status():
    """Delivery receipts for outbound messages.

    Without this the log keeps whatever status send time reported — 'queued' —
    so a text the carrier rejected looks sent forever.
    """
    sid    = request.form.get("MessageSid") or request.form.get("SmsSid") or ""
    status = request.form.get("MessageStatus") or request.form.get("SmsStatus") or ""
    if not sid or not status:
        return ("", 204)

    db  = get_db()
    row = db.execute(
        "SELECT lead_id, status FROM sms_log WHERE message_sid = ?", (sid,)
    ).fetchone()
    db.execute("UPDATE sms_log SET status = ? WHERE message_sid = ?", (status, sid))

    # A rejection is the only status worth a timeline entry: the rest are noise
    # the agent cannot act on.
    if status in FAILED_SMS_STATUSES and row and row["status"] not in FAILED_SMS_STATUSES:
        reason = request.form.get("ErrorCode") or ""
        log_activity(db, row["lead_id"], "sms",
                     f"Text {status}" + (f" (error {reason})" if reason else ""), sid)
    db.commit()
    return ("", 204)


@app.route("/sms_incoming", methods=["POST"])
@twilio_webhook
def sms_incoming():
    """Log an inbound text, honour opt-out keywords, and auto-create the lead.

    A reply from an unknown number is a warm inbound; creating the lead here
    means it is dialable from the CRM straight away instead of being lost in the
    Twilio console.
    """
    from_number = normalize_number(request.form.get("From", "")) or ""
    body        = (request.form.get("Body") or "").strip()
    sid         = request.form.get("MessageSid", "")

    db      = get_db()
    lead_id = lead_id_for_number(db, from_number)

    if not lead_id and from_number:
        cur = db.execute(
            "INSERT OR IGNORE INTO leads (phone, source, status, owner_id) VALUES (?, ?, ?, ?)",
            (from_number, "inbound-sms", "new", DEFAULT_OWNER),
        )
        lead_id = cur.lastrowid or lead_id_for_number(db, from_number)
        log_activity(db, lead_id, "created", "Created from an inbound text")

    if sid:
        _log_sms(db, sid, lead_id, "inbound", from_number, TWILIO_PHONE_NUMBER, body,
                 "received")

    if body.strip().lower().strip(".!") in OPT_OUT_KEYWORDS and lead_id:
        db.execute(
            "UPDATE leads SET dnc = 1, status = 'dnc', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (lead_id,),
        )
        db.execute("DELETE FROM campaign_leads WHERE lead_id = ?", (lead_id,))
        log_activity(db, lead_id, "optout", f"Opted out by text: {body!r}")
        app.logger.warning("Opt-out from %s — marked do-not-call", from_number)

    db.commit()
    return ("<Response></Response>", 200, {"Content-Type": "text/xml"})


# ══════════════════════════════════════════════════════════════════════════════
# CSV export
# ══════════════════════════════════════════════════════════════════════════════

def _csv_response(header, rows, filename):
    buf    = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/leads/export.csv", methods=["GET"])
def export_leads():
    rows = get_db().execute(
        "SELECT * FROM leads WHERE owner_id = ? ORDER BY created_at DESC",
        (DEFAULT_OWNER,),
    ).fetchall()
    header = ["name", "phone", "email", "company", "status", "tags", "dnc",
              "line_type", "carrier", "source", "notes", "created_at"]
    return _csv_response(
        header,
        [[r["name"], r["phone"], r["email"], r["company"], r["status"], r["tags"],
          int(r["dnc"] or 0), _get(r, "line_type") or "", _get(r, "carrier") or "",
          r["source"], (r["notes"] or "").replace("\n", " / "), r["created_at"]]
         for r in rows],
        f"leads-{datetime.now():%Y%m%d}.csv",
    )


@app.route("/api/calls/export.csv", methods=["GET"])
def export_calls():
    days = min(int(request.args.get("days", 90)), 365)
    rows = get_db().execute(
        """SELECT c.*, l.name FROM call_log c
           LEFT JOIN leads l ON l.id = c.lead_id
           WHERE c.owner_id = ? AND c.created_at >= datetime('now', ?)
           ORDER BY c.created_at DESC""",
        (DEFAULT_OWNER, f"-{days} days"),
    ).fetchall()
    header = ["call_sid", "date", "direction", "from", "to", "name", "status",
              "disposition", "duration_seconds", "answered_by", "note"]
    return _csv_response(
        header,
        [[r["call_sid"], r["created_at"], r["direction"], r["from_number"],
          r["to_number"], _get(r, "name") or "", r["status"], r["disposition"] or "",
          r["duration"] or 0, _get(r, "answered_by") or "",
          (r["note"] or "").replace("\n", " / ")]
         for r in rows],
        f"calls-{datetime.now():%Y%m%d}.csv",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Global search
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/search", methods=["GET"])
def global_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify({"leads": [], "calls": [], "tasks": []})

    db   = get_db()
    like = f"%{q}%"

    leads = db.execute(
        """SELECT id, name, phone, company, status FROM leads
           WHERE owner_id = ? AND (name LIKE ? OR phone LIKE ? OR company LIKE ?
                                   OR email LIKE ? OR notes LIKE ? OR tags LIKE ?)
           ORDER BY updated_at DESC LIMIT 20""",
        (DEFAULT_OWNER, like, like, like, like, like, like),
    ).fetchall()

    calls = db.execute(
        """SELECT c.call_sid, c.to_number, c.status, c.disposition, c.note,
                  c.created_at, l.name
           FROM call_log c LEFT JOIN leads l ON l.id = c.lead_id
           WHERE c.owner_id = ? AND (c.to_number LIKE ? OR c.note LIKE ?)
           ORDER BY c.created_at DESC LIMIT 20""",
        (DEFAULT_OWNER, like, like),
    ).fetchall()

    tasks = db.execute(
        """SELECT t.*, l.name, l.phone FROM tasks t
           LEFT JOIN leads l ON l.id = t.lead_id
           WHERE t.owner_id = ? AND t.title LIKE ?
           ORDER BY t.due_at LIMIT 20""",
        (DEFAULT_OWNER, like),
    ).fetchall()

    return jsonify({
        "leads": [dict(r) for r in leads],
        "calls": [dict(r) for r in calls],
        "tasks": [_task_row(r) for r in tasks],
    })


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

# ── Cost ──────────────────────────────────────────────────────────────────────
# Twilio does not put a price in the status callback: the amount lands on the
# call or message resource minutes later, once the carrier has rated the leg.
# Prices are therefore pulled in afterwards by /api/costs/sync, which the UI
# calls from the analytics view and which is safe to run from cron.

# How long to wait before asking again about a row Twilio has not priced yet.
PRICE_RETRY_HOURS = 6

# Statuses whose calls are finished, and so eligible for a price. An in-progress
# call has no final duration, let alone a rate.
FINAL_CALL_STATUSES = ("completed", "no-answer", "busy", "failed", "canceled")
_FINAL_CALL_SQL = "status IN (%s)" % ",".join(f"'{s}'" for s in FINAL_CALL_STATUSES)


def _price_of(resource):
    """Twilio prices are negative strings ('-0.00850') — a debit. Stored as a
    positive amount so summing them reads as money spent."""
    raw = getattr(resource, "price", None)
    if raw in (None, ""):
        return None, ""
    try:
        return abs(float(raw)), (getattr(resource, "price_unit", "") or "").upper()
    except (TypeError, ValueError):
        return None, ""


def _sync_prices(db, table, sid_column, fetch, where_extra, limit):
    """Fill in missing prices for one table. Returns (priced, attempted)."""
    stale = (datetime.now(timezone.utc)
             - timedelta(hours=PRICE_RETRY_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute(
        f"""SELECT {sid_column} sid FROM {table}
            WHERE owner_id = ? AND price IS NULL AND {where_extra}
                  AND (price_synced_at IS NULL OR price_synced_at < ?)
            ORDER BY created_at DESC LIMIT ?""",
        (DEFAULT_OWNER, stale, limit),
    ).fetchall()

    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    priced = 0
    for row in rows:
        try:
            price, unit = _price_of(fetch(row["sid"]))
        except Exception as e:
            # One unfetchable SID must not abandon the rest of the batch.
            app.logger.warning("price fetch failed for %s: %s", row["sid"], e)
            price, unit = None, ""
        if price is None:
            db.execute(
                f"UPDATE {table} SET price_synced_at = ? WHERE {sid_column} = ?",
                (now, row["sid"]),
            )
            continue
        db.execute(
            f"""UPDATE {table} SET price = ?, price_unit = ?, price_synced_at = ?
                WHERE {sid_column} = ?""",
            (price, unit, now, row["sid"]),
        )
        priced += 1

    db.commit()
    return priced, len(rows)


@app.route("/api/costs/sync", methods=["POST"])
def sync_costs():
    """Pull prices for calls and messages that do not have one yet.

    Bounded per request: pricing thousands of old rows is a background job's
    problem, and the caller can simply run it again.
    """
    limit  = min(int(request.args.get("limit", 50)), 200)
    db     = get_db()
    client = get_client()

    calls_priced, calls_tried = _sync_prices(
        db, "call_log", "call_sid", lambda sid: client.calls(sid).fetch(),
        _FINAL_CALL_SQL, limit,
    )

    msgs_priced, msgs_tried = _sync_prices(
        db, "sms_log", "message_sid", lambda sid: client.messages(sid).fetch(),
        "direction = 'outbound'", limit,
    )

    pending = db.execute(
        """SELECT (SELECT COUNT(*) FROM call_log
                    WHERE owner_id = ? AND price IS NULL) calls,
                  (SELECT COUNT(*) FROM sms_log
                    WHERE owner_id = ? AND price IS NULL
                          AND direction = 'outbound') messages""",
        (DEFAULT_OWNER, DEFAULT_OWNER),
    ).fetchone()

    return jsonify({
        "calls_priced":    calls_priced,
        "calls_checked":   calls_tried,
        "messages_priced": msgs_priced,
        "messages_checked": msgs_tried,
        "still_unpriced":  {"calls": pending["calls"], "messages": pending["messages"]},
    })


def cost_totals(db, win):
    """What the window cost, split by calls and messages.

    Rows Twilio has not priced yet are reported separately rather than counted as
    zero, so a total that is still filling in cannot be mistaken for a cheap day.
    """
    call_where, call_params = win.filter()
    calls = db.execute(
        f"""SELECT COALESCE(SUM(price), 0) spend,
                   SUM(CASE WHEN price IS NULL THEN 1 ELSE 0 END) unpriced,
                   MAX(price_unit) unit
            FROM call_log WHERE {call_where}""",
        call_params,
    ).fetchone()

    sms_where, sms_params = win.filter(campaign=False,
                                      extra=("direction = 'outbound'",))
    messages = db.execute(
        f"""SELECT COALESCE(SUM(price), 0) spend,
                   SUM(CASE WHEN price IS NULL THEN 1 ELSE 0 END) unpriced,
                   MAX(price_unit) unit
            FROM sms_log WHERE {sms_where}""",
        sms_params,
    ).fetchone()

    call_spend = round(calls["spend"] or 0, 4)
    sms_spend  = round(messages["spend"] or 0, 4)
    return {
        "calls":            call_spend,
        "messages":         sms_spend,
        "total":            round(call_spend + sms_spend, 4),
        "currency":         (calls["unit"] or messages["unit"] or "USD"),
        "unpriced_calls":    calls["unpriced"] or 0,
        "unpriced_messages": messages["unpriced"] or 0,
    }

# The SQL predicate for "the callee actually picked up". A call that is still
# in-progress has no final duration yet, so it is excluded from rate maths even
# though it counts as connected in the live UI.
_CONNECTED_SQL = "status IN ('completed','answered')"


def _connected(alias=""):
    return f"{alias}.{_CONNECTED_SQL}" if alias else _CONNECTED_SQL

# Below this many attempts an hour's connect rate is noise, not a signal, so it
# is excluded from "best time to call" rather than topping the chart at 100%.
MIN_ATTEMPTS_FOR_SIGNAL = 5

# Outcomes that mean the call moved the lead forward. Drives the funnel's last
# step, so it has to stay in sync with MANUAL_DISPOSITIONS.
POSITIVE_DISPOSITIONS = ("interested", "callback")

# Twilio message statuses that mean the carrier gave up on the message.
FAILED_SMS_STATUSES = ("failed", "undelivered")
_FAILED_SMS_SQL = "status IN (%s)" % ",".join(f"'{s}'" for s in FAILED_SMS_STATUSES)

# The longest custom range that will be served. Wider than the `days` presets
# because an explicit from/to is a deliberate request, not a default.
MAX_RANGE_DAYS = 366

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class Window:
    """A resolved analytics date range, in the viewer's timezone.

    Every timestamp in the database is UTC. Bucketing those into days and hours
    without shifting them first puts a 7pm call in the wrong day and makes
    "best hour to call" an hour nobody actually dialled, so a window carries the
    viewer's UTC offset and all its SQL shifts `created_at` by it.
    """

    def __init__(self, start, end, offset_minutes, campaign_id=None):
        self.start = start                    # inclusive, YYYY-MM-DD, local
        self.end = end                        # inclusive, YYYY-MM-DD, local
        self.offset = offset_minutes
        self.campaign_id = campaign_id

    @property
    def days(self):
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1

    @property
    def end_exclusive(self):
        """One day past the end, for `< end_exclusive` comparisons.

        'YYYY-MM-DD' sorts before every 'YYYY-MM-DD HH:MM:SS' on that date, so
        this is exact against the stored format without any time component.
        """
        return (date.fromisoformat(self.end) + timedelta(days=1)).isoformat()

    def local(self, column="created_at"):
        """`column` shifted from UTC into the viewer's timezone."""
        return f"datetime({column}, '{self.offset:+d} minutes')"

    def dates(self):
        """Every calendar day in the window, oldest first."""
        first = date.fromisoformat(self.start)
        return [(first + timedelta(days=n)).isoformat() for n in range(self.days)]

    def shifted_back(self):
        """The equally long window immediately before this one, for comparisons."""
        first = date.fromisoformat(self.start)
        return Window(
            (first - timedelta(days=self.days)).isoformat(),
            (first - timedelta(days=1)).isoformat(),
            self.offset, self.campaign_id,
        )

    def filter(self, table_alias="", extra=(), campaign=True):
        """(sql, params) restricting a query to this window's rows.

        `table_alias` prefixes the column names for queries that join, `extra`
        appends predicates that need no parameters, and `campaign=False` skips
        the campaign filter for tables that have no campaign_id column.
        """
        p = f"{table_alias}." if table_alias else ""
        local = f"datetime({p}created_at, '{self.offset:+d} minutes')"
        sql = [f"{p}owner_id = ?", f"{local} >= ?", f"{local} < ?"]
        params = [DEFAULT_OWNER, self.start, self.end_exclusive]
        if campaign and self.campaign_id is not None:
            sql.append(f"{p}campaign_id = ?")
            params.append(self.campaign_id)
        sql.extend(extra)
        return " AND ".join(sql), params


def _int_arg(name, default, low, high):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def resolve_window():
    """Build a Window from the request's query string.

    Accepts either `days` (a trailing preset, 1 meaning today only) or an
    explicit `from`/`to` pair. `tz` is the viewer's offset from UTC in minutes,
    matching JavaScript's `-new Date().getTimezoneOffset()`.
    """
    # ±14h covers every real zone, including the +14:00 Line Islands.
    offset = _int_arg("tz", 0, -840, 840)
    today  = (datetime.now(timezone.utc) + timedelta(minutes=offset)).date()

    campaign_id = request.args.get("campaign_id")
    campaign_id = int(campaign_id) if (campaign_id or "").isdigit() else None

    start_arg = (request.args.get("from") or "").strip()
    end_arg   = (request.args.get("to") or "").strip()
    if _DATE_RE.fullmatch(start_arg) and _DATE_RE.fullmatch(end_arg):
        try:
            first, last = date.fromisoformat(start_arg), date.fromisoformat(end_arg)
        except ValueError:
            first = last = None
        if first and last:
            if last < first:
                first, last = last, first
            # A range wider than the cap is trimmed from the start so the end
            # the user asked for is still the end they get.
            if (last - first).days + 1 > MAX_RANGE_DAYS:
                first = last - timedelta(days=MAX_RANGE_DAYS - 1)
            return Window(first.isoformat(), last.isoformat(), offset, campaign_id)

    days = _int_arg("days", 14, 1, 90)
    return Window((today - timedelta(days=days - 1)).isoformat(),
                  today.isoformat(), offset, campaign_id)


def _rate(part, whole):
    return round(part / whole * 100, 1) if whole else 0.0


def _bucket(rows, key):
    """Rows keyed by hour or weekday, with a connect rate attached to each."""
    return [
        {key: r[key], "calls": r["calls"], "connected": r["connected"] or 0,
         "rate": _rate(r["connected"] or 0, r["calls"])}
        for r in rows
    ]


def call_totals(db, win):
    """Scalar call metrics for one window. Shared by the current and previous
    period so a comparison is guaranteed to be like-for-like."""
    where, params = win.filter()
    row = db.execute(
        f"""SELECT COUNT(*) calls,
                   SUM(CASE WHEN {_CONNECTED_SQL} THEN 1 ELSE 0 END) connected,
                   COALESCE(SUM(duration), 0) talk_seconds
            FROM call_log WHERE {where}""",
        params,
    ).fetchone()

    calls     = row["calls"] or 0
    connected = row["connected"] or 0
    talk      = row["talk_seconds"] or 0
    return {
        "calls":        calls,
        "connected":    connected,
        "connect_rate": _rate(connected, calls),
        "talk_seconds": talk,
        "avg_duration": round(talk / connected) if connected else 0,
    }


def sms_totals(db, win):
    """Outbound volume, failures, and how often leads wrote back.

    Texts are not campaign-tagged, so a campaign filter cannot narrow them.
    """
    where, params = win.filter(campaign=False)
    row = db.execute(
        f"""SELECT
              SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) sent,
              SUM(CASE WHEN direction = 'inbound'  THEN 1 ELSE 0 END) received,
              SUM(CASE WHEN {_FAILED_SMS_SQL} THEN 1 ELSE 0 END) failed,
              SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END) delivered
            FROM sms_log WHERE {where}""",
        params,
    ).fetchone()

    # Reply rate is per lead, not per message: ten texts to one lead who answers
    # once is a 100% reply rate, not 10%.
    reach = db.execute(
        f"""SELECT
              COUNT(DISTINCT CASE WHEN direction = 'outbound' THEN lead_id END) texted,
              COUNT(DISTINCT CASE WHEN direction = 'inbound'  THEN lead_id END) replied
            FROM sms_log WHERE {where} AND lead_id IS NOT NULL""",
        params,
    ).fetchone()

    sent = row["sent"] or 0
    return {
        "sent":       sent,
        "received":   row["received"] or 0,
        "failed":     row["failed"] or 0,
        "delivered":  row["delivered"] or 0,
        # Only meaningful once delivery receipts are arriving; without a public
        # base URL there is no callback and this stays at zero.
        "delivery_rate": _rate(row["delivered"] or 0, sent),
        "texted":     reach["texted"] or 0,
        "replied":    reach["replied"] or 0,
        "reply_rate": _rate(reach["replied"] or 0, reach["texted"] or 0),
    }


def funnel_counts(db, win):
    """Lead-level progression: how many were dialled, reached, and advanced.

    Counted by distinct lead rather than by call, which is the only way the
    steps stay monotonically decreasing when a lead is dialled six times.
    """
    where, params = win.filter(extra=("lead_id IS NOT NULL",))
    placeholders = ",".join("?" * len(POSITIVE_DISPOSITIONS))
    row = db.execute(
        f"""SELECT
              COUNT(DISTINCT lead_id) attempted,
              COUNT(DISTINCT CASE WHEN {_CONNECTED_SQL} THEN lead_id END) connected,
              COUNT(DISTINCT CASE WHEN disposition IN ({placeholders})
                                  THEN lead_id END) advanced
            FROM call_log WHERE {where}""",
        # The disposition placeholders sit in the SELECT, which binds before the
        # WHERE clause's parameters.
        list(POSITIVE_DISPOSITIONS) + params,
    ).fetchone()
    return {
        "attempted": row["attempted"] or 0,
        "connected": row["connected"] or 0,
        "advanced":  row["advanced"] or 0,
    }


@app.route("/api/analytics", methods=["GET"])
def analytics():
    """Everything the analytics view draws, for one date window.

    Query params: `days` (1–90) or `from`/`to` (YYYY-MM-DD), `tz` (offset in
    minutes), `campaign_id`, and `compare=1` to include the preceding window's
    totals for deltas.
    """
    win = resolve_window()
    db  = get_db()

    totals = call_totals(db, win)
    where, params = win.filter()

    by_day_rows = db.execute(
        f"""SELECT date({win.local()}) day,
                   COUNT(*) calls,
                   SUM(CASE WHEN {_CONNECTED_SQL} THEN 1 ELSE 0 END) connected
            FROM call_log WHERE {where}
            GROUP BY day ORDER BY day""",
        params,
    ).fetchall()

    # A day with no calls is a real data point — leaving it out of the series
    # makes a quiet Sunday vanish and the chart lie about the trend.
    counted = {r["day"]: {"day": r["day"], "calls": r["calls"],
                          "connected": r["connected"] or 0}
               for r in by_day_rows}
    by_day  = [counted.get(d, {"day": d, "calls": 0, "connected": 0})
               for d in win.dates()]

    by_disposition = db.execute(
        f"""SELECT disposition, COUNT(*) n FROM call_log
            WHERE {where} AND disposition IS NOT NULL AND disposition != ''
            GROUP BY disposition ORDER BY n DESC""",
        params,
    ).fetchall()

    by_status = db.execute(
        f"""SELECT status, COUNT(*) n FROM call_log
            WHERE {where} AND status != '' GROUP BY status ORDER BY n DESC""",
        params,
    ).fetchall()

    by_hour = _bucket(db.execute(
        f"""SELECT CAST(strftime('%H', {win.local()}) AS INTEGER) hour,
                   COUNT(*) calls,
                   SUM(CASE WHEN {_CONNECTED_SQL} THEN 1 ELSE 0 END) connected
            FROM call_log WHERE {where} GROUP BY hour ORDER BY hour""",
        params,
    ).fetchall(), "hour")

    ranked    = [h for h in by_hour if h["calls"] >= MIN_ATTEMPTS_FOR_SIGNAL]
    best_hour = max(ranked, key=lambda h: h["rate"]) if ranked else None

    by_weekday = _bucket(db.execute(
        f"""SELECT CAST(strftime('%w', {win.local()}) AS INTEGER) weekday,
                   COUNT(*) calls,
                   SUM(CASE WHEN {_CONNECTED_SQL} THEN 1 ELSE 0 END) connected
            FROM call_log WHERE {where} GROUP BY weekday ORDER BY weekday""",
        params,
    ).fetchall(), "weekday")

    ranked_days  = [d for d in by_weekday if d["calls"] >= MIN_ATTEMPTS_FOR_SIGNAL]
    best_weekday = max(ranked_days, key=lambda d: d["rate"]) if ranked_days else None

    # Talk time and attempts per lead: who is soaking up the dials.
    lead_where, lead_params = win.filter("c", ("c.lead_id IS NOT NULL",))
    top_leads = db.execute(
        f"""SELECT c.lead_id, l.name, l.phone,
                   COUNT(*) calls,
                   SUM(CASE WHEN {_connected('c')} THEN 1 ELSE 0 END) connected,
                   COALESCE(SUM(c.duration), 0) talk_seconds
            FROM call_log c LEFT JOIN leads l ON l.id = c.lead_id
            WHERE {lead_where}
            GROUP BY c.lead_id ORDER BY calls DESC, talk_seconds DESC LIMIT 8""",
        lead_params,
    ).fetchall()

    # Recordings carry no campaign, so a campaign filter cannot narrow them and
    # they are counted for the window alone.
    recordings = db.execute(
        f"""SELECT COUNT(*) n FROM recordings
            WHERE owner_id = ? AND {win.local()} >= ? AND {win.local()} < ?""",
        (DEFAULT_OWNER, win.start, win.end_exclusive),
    ).fetchone()

    lead_totals = db.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN dnc = 1 THEN 1 ELSE 0 END) dnc,
                  SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) fresh
           FROM leads WHERE owner_id = ?""",
        (DEFAULT_OWNER,),
    ).fetchone()

    by_line_type = db.execute(
        """SELECT COALESCE(NULLIF(line_type, ''), 'unknown') line_type, COUNT(*) n
           FROM leads WHERE owner_id = ? GROUP BY line_type ORDER BY n DESC""",
        (DEFAULT_OWNER,),
    ).fetchall()

    by_lead_status = db.execute(
        """SELECT COALESCE(NULLIF(status, ''), 'new') status, COUNT(*) n
           FROM leads WHERE owner_id = ? GROUP BY status ORDER BY n DESC""",
        (DEFAULT_OWNER,),
    ).fetchall()

    task_totals = db.execute(
        """SELECT SUM(CASE WHEN done = 0 THEN 1 ELSE 0 END) open,
                  SUM(CASE WHEN done = 0 AND due_at <= ? THEN 1 ELSE 0 END) overdue,
                  SUM(CASE WHEN done = 1 THEN 1 ELSE 0 END) done
           FROM tasks WHERE owner_id = ?""",
        (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), DEFAULT_OWNER),
    ).fetchone()

    funnel = funnel_counts(db, win)
    funnel["leads_total"] = lead_totals["total"] or 0

    payload = {
        # `days` stays in the response because the UI labels the chart with it.
        "days":           win.days,
        "range":          {"from": win.start, "to": win.end, "tz_minutes": win.offset},
        "campaign_id":    win.campaign_id,
        **totals,
        "by_day":         by_day,
        "by_disposition": [dict(r) for r in by_disposition],
        "by_status":      [dict(r) for r in by_status],
        "by_hour":        by_hour,
        "by_weekday":     by_weekday,
        "best_hour":      best_hour,
        "best_weekday":   best_weekday,
        "top_leads":      [dict(r) for r in top_leads],
        "sms":            sms_totals(db, win),
        "cost":           cost_totals(db, win),
        "funnel":         funnel,
        "leads_total":    lead_totals["total"] or 0,
        "leads_dnc":      lead_totals["dnc"] or 0,
        "leads_new":      lead_totals["fresh"] or 0,
        "by_line_type":   [dict(r) for r in by_line_type],
        "by_lead_status": [dict(r) for r in by_lead_status],
        "tasks_open":     task_totals["open"] or 0,
        "tasks_overdue":  task_totals["overdue"] or 0,
        "tasks_done":     task_totals["done"] or 0,
        "recordings":     recordings["n"] or 0,
    }

    if request.args.get("compare") in ("1", "true", "yes"):
        previous = win.shifted_back()
        payload["previous"] = {
            "range": {"from": previous.start, "to": previous.end},
            **call_totals(db, previous),
            "sms": sms_totals(db, previous),
            "cost": cost_totals(db, previous),
        }

    return jsonify(payload)


@app.route("/api/analytics/export.csv", methods=["GET"])
def export_analytics():
    """The daily series as CSV, for the same window the view is showing."""
    win = resolve_window()
    db  = get_db()
    where, params = win.filter()

    rows = {
        r["day"]: r for r in db.execute(
            f"""SELECT date({win.local()}) day,
                       COUNT(*) calls,
                       SUM(CASE WHEN {_CONNECTED_SQL} THEN 1 ELSE 0 END) connected,
                       COALESCE(SUM(duration), 0) talk_seconds
                FROM call_log WHERE {where} GROUP BY day""",
            params,
        ).fetchall()
    }

    return _csv_response(
        ["date", "calls", "connected", "connect_rate_pct", "talk_seconds"],
        [[d,
          rows[d]["calls"] if d in rows else 0,
          (rows[d]["connected"] or 0) if d in rows else 0,
          _rate((rows[d]["connected"] or 0) if d in rows else 0,
                rows[d]["calls"] if d in rows else 0),
          rows[d]["talk_seconds"] if d in rows else 0]
         for d in win.dates()],
        f"analytics-{win.start}-to-{win.end}.csv",
    )


@app.route("/api/analytics/timing", methods=["GET"])
def analytics_timing():
    """Connect rate per hour-and-weekday cell — when this list actually picks up.

    Bucketed in the viewer's timezone via `tz`, same as /api/analytics.
    """
    win = resolve_window()
    where, params = win.filter()

    rows = get_db().execute(
        f"""SELECT CAST(strftime('%H', {win.local()}) AS INTEGER) hour,
                   CAST(strftime('%w', {win.local()}) AS INTEGER) weekday,
                   COUNT(*) calls,
                   SUM(CASE WHEN {_CONNECTED_SQL} THEN 1 ELSE 0 END) connected
            FROM call_log WHERE {where} GROUP BY hour, weekday""",
        params,
    ).fetchall()

    grid = [{"hour": r["hour"], "weekday": r["weekday"], "calls": r["calls"],
             "connected": r["connected"] or 0,
             "rate": _rate(r["connected"] or 0, r["calls"])}
            for r in rows]

    return jsonify({"days": win.days, "grid": grid,
                    "range": {"from": win.start, "to": win.end},
                    "min_attempts": MIN_ATTEMPTS_FOR_SIGNAL})


# ── Pickup odds ───────────────────────────────────────────────────────────────
# The dial clock says what time it is where the callee is; this says whether
# anyone answers at that hour. Same question, two halves, so they render on the
# same line above the keypad.
#
# Buckets are the CALLEE's local hour, not the agent's — which is what makes
# this different from /api/analytics/timing. One 9am dialling session reaches a
# New York lead at 9am and a Los Angeles lead at 6am; bucketing both under the
# agent's clock averages two unrelated human moments into a number that
# describes neither. Zones come from the number, and SQLite has no IANA database
# to convert with, so the bucket is computed per row in Python.

def _callee_local_hour(created_at, e164):
    """The hour of day it was where the callee was, when the call was placed."""
    zone = timezone_for_number(e164)
    if not zone or ZoneInfo is None:
        return None
    try:
        stamp = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return stamp.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone)).hour


@app.route("/api/analytics/pickup", methods=["GET"])
def analytics_pickup():
    """Connect rate at the hour it currently is for `number`, from call history.

    Hours below MIN_ATTEMPTS_FOR_SIGNAL attempts report `signal: false` and are
    left out of `best_hours`: three-for-three is not a 100% pickup rate, and
    showing it as one would send the agent chasing noise.
    """
    number = normalize_number(request.args.get("number", ""))
    if not number:
        return jsonify({"error": "invalid number"}), 400

    win           = resolve_window()
    where, params = win.filter(extra=("direction LIKE 'outbound%'",))

    rows = get_db().execute(
        f"""SELECT created_at, to_number,
                   CASE WHEN {_CONNECTED_SQL} THEN 1 ELSE 0 END connected
            FROM call_log WHERE {where}""",
        params,
    ).fetchall()

    # hour -> [attempts, connected]
    buckets = {}
    for r in rows:
        hour = _callee_local_hour(r["created_at"], r["to_number"])
        if hour is None:                  # unmapped zone: no honest bucket for it
            continue
        slot = buckets.setdefault(hour, [0, 0])
        slot[0] += 1
        slot[1] += r["connected"]

    local = local_time_for_number(number)
    hour  = local.hour if local else None
    here  = buckets.get(hour) if hour is not None else None

    attempts  = sum(a for a, _ in buckets.values())
    connected = sum(c for _, c in buckets.values())

    hours = [{"hour": h, "calls": a, "connected": c, "rate": _rate(c, a)}
             for h, (a, c) in sorted(buckets.items())]
    best = sorted((h for h in hours if h["calls"] >= MIN_ATTEMPTS_FOR_SIGNAL),
                  key=lambda h: (-h["rate"], h["hour"]))[:3]

    return jsonify({
        "number":       number,
        "timezone":     timezone_for_number(number) or "",
        "hour":         hour,
        "calls":        here[0] if here else 0,
        "connected":    here[1] if here else 0,
        "rate":         _rate(here[1], here[0]) if here else 0.0,
        "signal":       bool(here and here[0] >= MIN_ATTEMPTS_FOR_SIGNAL),
        "overall_rate": _rate(connected, attempts),
        "sample":       attempts,
        "hours":        hours,
        "best_hours":   best,
        "days":         win.days,
        "min_attempts": MIN_ATTEMPTS_FOR_SIGNAL,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
