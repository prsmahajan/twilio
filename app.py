import os
from flask import Flask, request, jsonify, send_from_directory
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Dial
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static/dist', static_url_path='')

@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')

TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_API_KEY      = os.environ["TWILIO_API_KEY"]
TWILIO_API_SECRET   = os.environ["TWILIO_API_SECRET"]
TWILIO_APP_SID      = os.environ["TWILIO_APP_SID"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]

_client = None

def get_client():
    global _client
    if _client is None:
        _client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return _client


# ── Voice token ───────────────────────────────────────────────────────────────

@app.route("/token", methods=["GET"])
def token():
    try:
        grant = VoiceGrant(outgoing_application_sid=TWILIO_APP_SID, incoming_allow=False)
        access_token = AccessToken(
            TWILIO_ACCOUNT_SID, TWILIO_API_KEY, TWILIO_API_SECRET,
            identity="dialer-user", ttl=3600,
        )
        access_token.add_grant(grant)
        return jsonify({"token": access_token.to_jwt()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── TwiML webhook ─────────────────────────────────────────────────────────────

@app.route("/connect", methods=["POST"])
def connect():
    to_number = request.form.get("To", "").strip()
    response  = VoiceResponse()
    if not to_number:
        response.say("No destination number provided.")
        return str(response), 200, {"Content-Type": "text/xml"}
    dial = Dial(caller_id=TWILIO_PHONE_NUMBER, answer_on_bridge=True)
    dial.number(to_number)
    response.append(dial)
    return str(response), 200, {"Content-Type": "text/xml"}


# ── SMS threads (one entry per contact, most recent message) ──────────────────

@app.route("/threads", methods=["GET"])
def threads():
    try:
        client   = get_client()
        sent     = list(client.messages.list(from_=TWILIO_PHONE_NUMBER, limit=100))
        received = list(client.messages.list(to=TWILIO_PHONE_NUMBER,   limit=100))

        thread_map = {}
        for msg in sent + received:
            msg_from = getattr(msg, 'from_', None) or getattr(msg, 'from', '')
            contact  = msg.to if msg_from == TWILIO_PHONE_NUMBER else msg_from
            date     = msg.date_sent
            if contact not in thread_map or (date and thread_map[contact]["_dt"] and date > thread_map[contact]["_dt"]):
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
        return jsonify({"error": str(e)}), 500


# ── SMS conversation with a single contact ────────────────────────────────────

@app.route("/messages", methods=["GET"])
def messages():
    contact = request.args.get("contact", "").strip()
    if not contact:
        return jsonify([])
    try:
        client   = get_client()
        sent     = list(client.messages.list(from_=TWILIO_PHONE_NUMBER, to=contact, limit=50))
        received = list(client.messages.list(from_=contact, to=TWILIO_PHONE_NUMBER, limit=50))
        all_msgs = []
        for msg in sent + received:
            msg_from = getattr(msg, 'from_', None) or getattr(msg, 'from', '')
            all_msgs.append({
                "sid":       msg.sid,
                "body":      msg.body or "",
                "direction": "outbound" if msg_from == TWILIO_PHONE_NUMBER else "inbound",
                "date":      msg.date_sent.isoformat() if msg.date_sent else "",
            })
        all_msgs.sort(key=lambda x: x["date"])
        return jsonify(all_msgs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Send SMS ──────────────────────────────────────────────────────────────────

@app.route("/send_sms", methods=["POST"])
def send_sms():
    data = request.get_json() or {}
    to   = data.get("to",   "").strip()
    body = data.get("body", "").strip()
    if not to or not body:
        return jsonify({"error": "missing to or body"}), 400
    try:
        client = get_client()
        msg    = client.messages.create(to=to, from_=TWILIO_PHONE_NUMBER, body=body)
        return jsonify({"sid": msg.sid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Recent calls ──────────────────────────────────────────────────────────────

@app.route("/recent", methods=["GET"])
def recent():
    try:
        client    = get_client()
        outbound  = list(client.calls.list(from_=TWILIO_PHONE_NUMBER, limit=50))
        inbound   = list(client.calls.list(to=TWILIO_PHONE_NUMBER,   limit=50))
        seen, result = set(), []
        for call in outbound + inbound:
            if call.sid in seen:
                continue
            seen.add(call.sid)
            from_num = getattr(call, 'from_', None) or ''
            to_num   = getattr(call, 'to',    None) or ''
            result.append({
                "to":        to_num,
                "from_":     from_num,
                "direction": call.direction,
                "status":    call.status,
                "duration":  call.duration,
                "date":      call.date_created.isoformat() if call.date_created else "",
            })
        result.sort(key=lambda x: x["date"], reverse=True)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
