import os
import csv
import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for
from twilio.twiml.voice_response import VoiceResponse, Dial
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_PHONE_NUMBER = os.environ["TWILIO_PHONE_NUMBER"]
TWILIO_API_KEY     = os.environ["TWILIO_API_KEY"]
TWILIO_API_SECRET  = os.environ["TWILIO_API_SECRET"]
TWILIO_APP_SID     = os.environ["TWILIO_APP_SID"]

LEADS_FILE = "leads.csv"
NOTES_FILE = "notes.csv"


def read_leads():
    leads = []
    if not os.path.exists(LEADS_FILE):
        return leads
    with open(LEADS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append(row)
    return leads


def read_notes():
    notes = {}
    if not os.path.exists(NOTES_FILE):
        return notes
    with open(NOTES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lead_id = row["lead_id"]
            if lead_id not in notes:
                notes[lead_id] = []
            notes[lead_id].append(row)
    return notes


def append_note(lead_id, lead_name, note_text):
    file_exists = os.path.exists(NOTES_FILE)
    with open(NOTES_FILE, "a", newline="", encoding="utf-8") as f:
        fieldnames = ["lead_id", "lead_name", "note", "timestamp"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "lead_id": lead_id,
            "lead_name": lead_name,
            "note": note_text,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })


@app.route("/")
def index():
    leads = read_leads()
    notes = read_notes()
    return render_template("index.html", leads=leads, notes=notes)


@app.route("/token", methods=["GET"])
def token():
    """Return a short-lived Twilio Access Token for the browser Voice SDK."""
    grant = VoiceGrant(
        outgoing_application_sid=TWILIO_APP_SID,
        incoming_allow=False,
    )
    access_token = AccessToken(
        TWILIO_ACCOUNT_SID,
        TWILIO_API_KEY,
        TWILIO_API_SECRET,
        identity="dialer-user",
        ttl=3600,
    )
    access_token.add_grant(grant)
    return jsonify({"token": access_token.to_jwt()})


@app.route("/connect", methods=["GET", "POST"])
def connect():
    """TwiML webhook called by Twilio when the browser places a call via the SDK."""
    to_number = request.form.get("To") or request.args.get("To", "")
    response = VoiceResponse()

    if not to_number:
        response.say("No destination number provided.")
        return str(response), 200, {"Content-Type": "text/xml"}

    dial = Dial(caller_id=TWILIO_PHONE_NUMBER, answer_on_bridge=True)
    dial.number(to_number)
    response.append(dial)

    return str(response), 200, {"Content-Type": "text/xml"}


@app.route("/save_note", methods=["POST"])
def save_note():
    lead_id = request.form.get("lead_id", "").strip()
    lead_name = request.form.get("lead_name", "").strip()
    note_text = request.form.get("note", "").strip()

    if not lead_id or not note_text:
        return redirect(url_for("index"))

    append_note(lead_id, lead_name, note_text)
    return redirect(url_for("index") + f"#lead-{lead_id}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
