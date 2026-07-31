"""Exercise the additions: recording, AMD, Lookup enrichment, calling-window
compliance, follow-up tasks, templates, bulk SMS, inbound SMS and exports."""
import os

os.environ.update({
    "TWILIO_ACCOUNT_SID":  "AC" + "0" * 32,
    "TWILIO_AUTH_TOKEN":   "test_auth_token_12345",
    "TWILIO_API_KEY":      "SK" + "0" * 32,
    "TWILIO_API_SECRET":   "secret",
    "TWILIO_APP_SID":      "AP" + "0" * 32,
    "TWILIO_PHONE_NUMBER": "+15550000000",
    "PUBLIC_BASE_URL":     "https://dialer.example.com",
    "DB_PATH":             "/tmp/test_extras.db",
    # Every optional feature on, so the TwiML and gating assertions below are
    # exercising the configured path rather than the default no-op one.
    "RECORD_CALLS":         "true",
    "AMD_ENABLED":          "true",
    "VOICEMAIL_MESSAGE":    "Sorry we missed you.",
    "QUIET_HOURS_ENFORCED": "true",
})

import app as A
from datetime import date, timedelta
from twilio.request_validator import RequestValidator

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r}")
    if not ok:
        fails.append(label)
        print(f"      wanted {want!r}")


c = A.app.test_client()


def signed(path, params):
    url = "https://dialer.example.com" + path
    sig = RequestValidator("test_auth_token_12345").compute_signature(url, params)
    return c.post(path, data=params, headers={"X-Twilio-Signature": sig})


print("=== feature flags ===")
cfg = c.get("/api/config").get_json()
check("recording advertised", cfg["recording"], True)
check("amd advertised", cfg["amd"], True)
check("voicemail drop advertised", cfg["voicemail_drop"], True)
check("no secrets leaked", any(k for k in cfg if "token" in k or "sid" in k), False)

print("\n=== timezone inference from area code ===")
check("NYC is Eastern", A.timezone_for_number("+12125551212"), "America/New_York")
check("SF is Pacific", A.timezone_for_number("+14155551212"), "America/Los_Angeles")
check("Phoenix has no DST zone", A.timezone_for_number("+16025551212"), "America/Phoenix")
check("malformed unknown", A.timezone_for_number("+1212555"), None)

print("\n=== timezone inference from country code ===")
check("UK resolves", A.timezone_for_number("+442071234567"), "Europe/London")
check("India resolves", A.timezone_for_number("+919876543210"), "Asia/Kolkata")
check("three digit code resolves", A.timezone_for_number("+9715012345678"), "Asia/Dubai")
# Multi-zone countries are deliberately unmapped: a confident wrong clock is
# worse than admitting the zone is unknown.
check("multi-zone country stays unknown", A.timezone_for_number("+61255512345"), None)
check("country name for +91", A.country_for_number("+919876543210")[0], "India")
check("NANP country is US/Canada", A.country_for_number("+12125551212"),
      ("US/Canada", "America/New_York"))
check("country code longest match wins",
      A.country_code_for_number("+9715012345678"), "971")

print("\n=== calling window ===")
# An unknown timezone must not block the call — refusing every number in an
# unmapped country would be worse than the compliance risk it avoids.
check("unknown timezone is allowed", A.call_window_status("+61255512345")[0], True)
check("unknown timezone says why", A.call_window_status("+61255512345")[1],
      "timezone unknown")

print("\n=== outbound TwiML carries recording and AMD ===")
# /connect refuses to dial outside the callee's calling window, so with quiet
# hours on this assertion would only pass between 08:00 and 21:00 Eastern. The
# window itself is covered above; here it is switched off so the TwiML is
# checked at any hour of the day.
_quiet = A.QUIET_HOURS_ENFORCED
A.QUIET_HOURS_ENFORCED = False
twiml = signed("/connect", {"To": "2125551212", "From": "client:dialer-user"}) \
    .get_data(as_text=True)
A.QUIET_HOURS_ENFORCED = _quiet
check("records dual channel", 'record="record-from-answer-dual"' in twiml, True)
check("recording callback set",
      'recordingStatusCallback="https://dialer.example.com/recording_status"' in twiml,
      True)
check("AMD enabled", 'machineDetection="DetectMessageEnd"' in twiml, True)
check("AMD callback set",
      'amdStatusCallback="https://dialer.example.com/amd_status"' in twiml, True)

print("\n=== voicemail TwiML ===")
vm = signed("/voicemail_drop", {}).get_data(as_text=True)
check("speaks the message", "<Say>Sorry we missed you.</Say>" in vm, True)
check("hangs up after", "<Hangup" in vm, True)

print("\n=== AMD verdict is recorded ===")
sid = "CA" + "1" * 32
check("amd webhook accepted",
      signed("/amd_status", {"CallSid": sid, "AnsweredBy": "machine_end_beep"}).status_code,
      204)
with A.app.app_context():
    row = A.get_db().execute(
        "SELECT answered_by FROM call_log WHERE call_sid = ?", (sid,)).fetchone()
check("answered_by stored", row["answered_by"], "machine_end_beep")

print("\n=== unsigned webhooks are refused ===")
for path in ("/amd_status", "/recording_status", "/voicemail_drop", "/sms_incoming",
             "/sms_status"):
    check(f"{path} rejects unsigned", c.post(path, data={}).status_code, 403)

print("\n=== recordings ===")
rec_sid = "RE" + "a" * 32
check("recording webhook accepted",
      signed("/recording_status", {
          "RecordingSid": rec_sid, "CallSid": sid,
          "RecordingDuration": "42", "RecordingChannels": "2",
          "RecordingStatus": "completed",
      }).status_code, 204)
recs = c.get("/api/recordings").get_json()
check("recording listed", len(recs), 1)
check("duration stored", recs[0]["duration"], 42)
check("dual channel stored", recs[0]["channels"], 2)
check("bogus sid refused",
      c.get("/api/recordings/not-a-sid/audio").status_code, 400)

# The callback's own RecordingUrl is what the audio proxy must use: rebuilding
# it from TWILIO_ACCOUNT_SID 404s for calls that ran under a subaccount.
sub_sid = "RE" + "b" * 32
sub_url = "https://api.twilio.com/2010-04-01/Accounts/ACsubaccount/Recordings/" + sub_sid
signed("/recording_status", {
    "RecordingSid": sub_sid, "CallSid": sid, "RecordingDuration": "7",
    "RecordingStatus": "completed", "RecordingUrl": sub_url,
})

fetched = []


class _FakeUpstream:
    status_code = 200
    headers = {"Content-Type": "audio/mpeg", "Content-Length": "3"}

    def iter_content(self, chunk_size=1):
        yield b"abc"

    def close(self):
        pass


def _fake_get(url, **kwargs):
    fetched.append((url, (kwargs.get("headers") or {}).get("Range")))
    return _FakeUpstream()


_real_get = A.requests.get
A.requests.get = _fake_get
audio = c.get(f"/api/recordings/{sub_sid}/audio", headers={"Range": "bytes=0-1"})
A.requests.get = _real_get
check("audio proxied", audio.status_code, 200)
check("subaccount media url preferred", fetched[0][0], sub_url + ".mp3")
check("range forwarded upstream", fetched[0][1], "bytes=0-1")
check("seeking advertised", audio.headers.get("Accept-Ranges"), "bytes")

print("\n=== leads gain enrichment fields ===")
lead = c.post("/api/leads", json={"name": "Ada Lovelace", "phone": "2125551212",
                                  "company": "Analytical"}).get_json()
lid = lead["id"]
check("timezone inferred on read", lead["timezone"], "America/New_York")
check("valid is unknown before lookup", lead["valid"], None)

print("\n=== compliance check ===")
comp = c.get("/api/compliance/check?number=2125551212").get_json()
check("normalizes the number", comp["number"], "+12125551212")
check("reports the zone", comp["timezone"], "America/New_York")
check("not on dnc", comp["dnc"], False)
check("bad number refused", c.get("/api/compliance/check?number=123").status_code, 400)

print("\n=== follow-up tasks ===")
t = c.post("/api/tasks", json={"lead_id": lid, "title": "Call back", "due_at": "+2h"})
check("task created", t.status_code, 201)
check("task attached to lead", t.get_json()["lead_id"], lid)
check("future task is not overdue", t.get_json()["overdue"], False)

past = c.post("/api/tasks", json={"title": "Overdue one",
                                  "due_at": "2020-01-01T00:00:00Z"}).get_json()
check("past task is overdue", past["overdue"], True)
check("overdue scope finds it",
      [x["id"] for x in c.get("/api/tasks?scope=overdue").get_json()], [past["id"]])
check("open scope finds both", len(c.get("/api/tasks?scope=open").get_json()), 2)

check("bad due_at refused",
      c.post("/api/tasks", json={"title": "x", "due_at": "whenever"}).status_code, 400)
check("titleless task refused",
      c.post("/api/tasks", json={"due_at": "+1d"}).status_code, 400)

c.patch(f"/api/tasks/{past['id']}", json={"done": True})
check("done scope finds the completed one",
      [x["id"] for x in c.get("/api/tasks?scope=done").get_json()], [past["id"]])
check("task deleted", c.delete(f"/api/tasks/{past['id']}").status_code, 204)

print("\n=== templates and merge fields ===")
tpl = c.post("/api/templates", json={
    "name": "Intro", "body": "Hi {{first_name}} at {{company}}, got a sec? {{nope}}"})
check("template created", tpl.status_code, 201)
check("duplicate name refused",
      c.post("/api/templates", json={"name": "Intro", "body": "x"}).status_code, 409)

prev = c.post("/api/templates/preview",
              json={"lead_id": lid, "body": tpl.get_json()["body"]}).get_json()
check("first name substituted", "Hi Ada at Analytical" in prev["body"], True)
# An unknown placeholder is left visible rather than blanked, so a typo shows up
# in the preview instead of sending a sentence with a hole in it.
check("unknown field left intact", "{{nope}}" in prev["body"], True)
check("preview reports the length", prev["length"], len(prev["body"]))
check("preview knows the cap", prev["max_length"], A.SMS_MAX_LENGTH)
check("short preview is not too long", prev["too_long"], False)

print("\n=== one SMS segment is enforced ===")
check("cap advertised to the UI", cfg["sms_max_length"], 160)
long_body = "x" * (A.SMS_MAX_LENGTH + 1)
over = c.post("/send_sms", json={"to": "2125551212", "body": long_body})
check("over-length send refused", over.status_code, 400)
check("refusal states the length", over.get_json()["length"], A.SMS_MAX_LENGTH + 1)
check("refusal states the cap", over.get_json()["max_length"], A.SMS_MAX_LENGTH)
check("exactly at the cap is not refused",
      "characters" in (c.post("/send_sms", json={"to": "2125551212",
                                                 "body": "x" * A.SMS_MAX_LENGTH})
                       .get_json().get("error") or ""), False)
check("over-length template refused",
      c.post("/api/templates", json={"name": "Long", "body": long_body}).status_code, 400)
check("over-length template edit refused",
      c.patch(f"/api/templates/{tpl.get_json()['id']}",
              json={"body": long_body}).status_code, 400)
check("over-length bulk refused",
      c.post("/api/sms/bulk", json={"body": long_body, "lead_ids": [lid]}).status_code,
      400)

print("\n=== delivery receipts ===")
msg_sid = "SM" + "c" * 32
with A.app.app_context():
    db = A.get_db()
    A._log_sms(db, msg_sid, lid, "outbound", "+15550000000", "+12125551212",
               "hi", "queued")
    db.commit()
check("status webhook accepted",
      signed("/sms_status", {"MessageSid": msg_sid, "MessageStatus": "delivered"})
      .status_code, 204)
with A.app.app_context():
    row = A.get_db().execute(
        "SELECT status FROM sms_log WHERE message_sid = ?", (msg_sid,)).fetchone()
check("delivery status stored", row["status"], "delivered")
check("delivered counted in analytics",
      c.get("/api/analytics?days=1").get_json()["sms"]["delivered"], 1)

failed_sid = "SM" + "d" * 32
with A.app.app_context():
    db = A.get_db()
    A._log_sms(db, failed_sid, lid, "outbound", "+15550000000", "+12125551212",
               "hi", "queued")
    db.commit()
signed("/sms_status", {"MessageSid": failed_sid, "MessageStatus": "undelivered",
                       "ErrorCode": "30006"})
with A.app.app_context():
    events = A.get_db().execute(
        "SELECT body FROM activities WHERE ref = ?", (failed_sid,)).fetchall()
check("a rejected text lands on the timeline", len(events), 1)
check("the error code is kept", "30006" in events[0]["body"], True)

print("\n=== per-call and per-message cost ===")
# Only finished calls can be priced, so the sync needs one to work on.
priced_call = "CA" + "e" * 32
with A.app.app_context():
    db = A.get_db()
    db.execute(
        """INSERT INTO call_log (call_sid, to_number, from_number, status, duration,
                                 lead_id, owner_id)
           VALUES (?, ?, ?, 'completed', 31, ?, 'default')""",
        (priced_call, "+12125551212", "+15550000000", lid),
    )
    db.commit()


class _FakePriced:
    def __init__(self, price):
        self.price = price
        self.price_unit = "usd"


class _FakeTwilio:
    """Only the two lookups the cost sync performs."""

    def calls(self, sid):
        return type("R", (), {"fetch": staticmethod(lambda: _FakePriced("-0.0085"))})

    def messages(self, sid):
        return type("R", (), {"fetch": staticmethod(lambda: _FakePriced("-0.0079"))})


_real_client = A.get_client
A.get_client = lambda: _FakeTwilio()
synced = c.post("/api/costs/sync").get_json()
A.get_client = _real_client
check("calls priced", synced["calls_priced"] >= 1, True)
check("messages priced", synced["messages_priced"] >= 1, True)

cost = c.get("/api/analytics?days=1").get_json()["cost"]
check("call spend is a positive amount", cost["calls"] > 0, True)
check("message spend is a positive amount", cost["messages"] > 0, True)
check("total is the sum",
      cost["total"], round(cost["calls"] + cost["messages"], 4))
check("currency normalised", cost["currency"], "USD")
check("messages all priced", cost["unpriced_messages"], 0)
with A.app.app_context():
    row = A.get_db().execute(
        "SELECT price, price_unit FROM call_log WHERE call_sid = ?",
        (priced_call,)).fetchone()
check("the finished call carries its price", (row["price"], row["price_unit"]),
      (0.0085, "USD"))
# A call with no final status yet cannot be priced, and is reported as pending
# rather than counted as free.
check("unfinished calls stay pending", cost["unpriced_calls"] >= 1, True)
check("price stored as a debit-free positive", A._price_of(_FakePriced("-0.0085")),
      (0.0085, "USD"))
check("missing price stays unknown", A._price_of(_FakePriced(None)), (None, ""))

print("\n=== notes and timeline ===")
check("note added",
      c.post(f"/api/leads/{lid}/notes", json={"body": "Talked shop"}).status_code, 201)
check("empty note refused",
      c.post(f"/api/leads/{lid}/notes", json={"body": "  "}).status_code, 400)
kinds = [i["kind"] for i in c.get(f"/api/leads/{lid}/timeline").get_json()]
check("timeline has the note", "note" in kinds, True)
check("timeline has the task", "task" in kinds, True)

print("\n=== inbound SMS creates the lead and honours opt-out ===")
r = signed("/sms_incoming", {"From": "+13105551212", "Body": "Tell me more",
                             "MessageSid": "SM" + "b" * 32})
check("inbound accepted", r.status_code, 200)
new_lead = [l for l in c.get("/api/leads").get_json()["leads"]
            if l["phone"] == "+13105551212"]
check("lead auto-created", len(new_lead), 1)
check("source recorded", new_lead[0]["source"], "inbound-sms")
check("not opted out yet", new_lead[0]["dnc"], False)

signed("/sms_incoming", {"From": "+13105551212", "Body": "STOP",
                         "MessageSid": "SM" + "c" * 32})
opted = [l for l in c.get("/api/leads").get_json()["leads"]
         if l["phone"] == "+13105551212"][0]
check("STOP sets do-not-call", opted["dnc"], True)
check("STOP sets the status", opted["status"], "dnc")

# The whole point of the flag: a do-not-call lead must be unreachable by voice.
blocked = signed("/connect", {"To": "3105551212", "From": "client:dialer-user"}) \
    .get_data(as_text=True)
check("dnc lead is not dialed", "<Dial" in blocked, False)
check("dnc lead is told why", "do not call list" in blocked, True)

print("\n=== lead filters ===")
check("uncalled filter", len(c.get("/api/leads?uncalled=1").get_json()["leads"]), 2)
check("dnc filter", len(c.get("/api/leads?dnc=1").get_json()["leads"]), 1)
check("name sort", [l["name"] for l in
                    c.get("/api/leads?sort=name&dnc=0").get_json()["leads"]],
      ["Ada Lovelace"])

print("\n=== search ===")
res = c.get("/api/search?q=Ada").get_json()
check("finds the lead", [l["name"] for l in res["leads"]], ["Ada Lovelace"])
check("short query returns nothing", c.get("/api/search?q=A").get_json()["leads"], [])

print("\n=== CSV export ===")
leads_csv = c.get("/api/leads/export.csv")
check("csv content type", leads_csv.mimetype, "text/csv")
check("csv is an attachment",
      "attachment" in leads_csv.headers["Content-Disposition"], True)
check("csv has the lead", "Ada Lovelace" in leads_csv.get_data(as_text=True), True)
check("csv has the enrichment columns",
      "line_type,carrier" in leads_csv.get_data(as_text=True), True)
check("calls csv served", c.get("/api/calls/export.csv").mimetype, "text/csv")

print("\n=== analytics additions ===")
a = c.get("/api/analytics").get_json()
for key in ("by_hour", "by_weekday", "best_hour", "by_line_type",
            "tasks_open", "tasks_overdue", "recordings"):
    check(f"analytics exposes {key}", key in a, True)
check("open tasks counted", a["tasks_open"], 1)
check("recordings counted", a["recordings"], 2)
check("timing endpoint", "grid" in c.get("/api/analytics/timing").get_json(), True)

print("\n=== analytics windows and filters ===")
for key in ("range", "sms", "funnel", "top_leads", "by_lead_status",
            "best_weekday", "campaign_id"):
    check(f"analytics exposes {key}", key in a, True)

today = date.today().isoformat()
one = c.get("/api/analytics?days=1").get_json()
check("one day window is one bucket", len(one["by_day"]), 1)
check("one day window ends today", one["range"]["to"], today)
check("every day in the range is present", len(c.get("/api/analytics?days=7")
      .get_json()["by_day"]), 7)

custom = c.get("/api/analytics?from=2026-01-01&to=2026-01-10").get_json()
check("custom range honoured", [custom["range"]["from"], custom["range"]["to"]],
      ["2026-01-01", "2026-01-10"])
check("custom range day count", custom["days"], 10)
check("reversed custom range is swapped",
      c.get("/api/analytics?from=2026-01-10&to=2026-01-01").get_json()["range"]["from"],
      "2026-01-01")
check("garbage dates fall back to days",
      c.get("/api/analytics?from=nope&to=also-nope&days=3").get_json()["days"], 3)
check("days is clamped to the cap",
      c.get("/api/analytics?days=9999").get_json()["days"], 90)
check("days below one is clamped up",
      c.get("/api/analytics?days=0").get_json()["days"], 1)
check("custom range is capped",
      c.get("/api/analytics?from=2000-01-01&to=2026-01-01").get_json()["days"],
      A.MAX_RANGE_DAYS)

# A timezone offset shifts the day and hour buckets, not the totals.
shifted = c.get("/api/analytics?days=7&tz=330").get_json()
check("tz echoed back", shifted["range"]["tz_minutes"], 330)
check("tz is clamped to a real offset",
      c.get("/api/analytics?tz=99999").get_json()["range"]["tz_minutes"], 840)

check("campaign filter echoed",
      c.get("/api/analytics?campaign_id=1").get_json()["campaign_id"], 1)
check("no campaign filter by default", a["campaign_id"], None)

funnel = a["funnel"]
check("funnel never grows down the steps",
      [funnel["leads_total"] >= funnel["attempted"],
       funnel["attempted"] >= funnel["connected"],
       funnel["connected"] >= funnel["advanced"]], [True, True, True])

sms = a["sms"]
for key in ("sent", "received", "failed", "texted", "replied", "reply_rate"):
    check(f"sms totals expose {key}", key in sms, True)

compared = c.get("/api/analytics?days=7&compare=1").get_json()
check("comparison included on request", "previous" in compared, True)
check("comparison covers the preceding window",
      compared["previous"]["range"]["to"],
      (date.fromisoformat(compared["range"]["from"]) - timedelta(days=1)).isoformat())
check("comparison omitted by default", "previous" in a, False)

csv_out = c.get("/api/analytics/export.csv?days=3")
check("analytics csv served", csv_out.mimetype, "text/csv")
check("analytics csv has a row per day",
      len(csv_out.get_data(as_text=True).strip().splitlines()), 4)
check("analytics csv names its range",
      f"analytics-" in csv_out.headers["Content-Disposition"], True)

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
raise SystemExit(1 if fails else 0)
