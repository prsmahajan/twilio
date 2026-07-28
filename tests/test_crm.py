"""Exercise the CRM: leads, import, campaigns, dispositions, analytics."""
import os

os.environ.update({
    "TWILIO_ACCOUNT_SID":  "AC" + "0" * 32,
    "TWILIO_AUTH_TOKEN":   "test_auth_token_12345",
    "TWILIO_API_KEY":      "SK" + "0" * 32,
    "TWILIO_API_SECRET":   "secret",
    "TWILIO_APP_SID":      "AP" + "0" * 32,
    "TWILIO_PHONE_NUMBER": "+15550000000",
    "PUBLIC_BASE_URL":     "https://dialer.example.com",
    "DB_PATH":             "/tmp/test_crm.db",
})

import app as A
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


print("=== leads CRUD ===")
r = c.post("/api/leads", json={"name": "Ada Lovelace", "phone": "5551234567",
                               "company": "Analytical Eng"})
check("create lead", r.status_code, 201)
lead = r.get_json()
check("phone normalized to E.164", lead["phone"], "+15551234567")
lead_id = lead["id"]

r = c.post("/api/leads", json={"name": "Dup", "phone": "(555) 123-4567"})
check("duplicate phone rejected", r.status_code, 409)

r = c.post("/api/leads", json={"name": "Bad", "phone": "nonsense"})
check("invalid phone rejected", r.status_code, 400)

r = c.get("/api/leads?q=Ada")
check("search finds lead", len(r.get_json()["leads"]), 1)
r = c.get("/api/leads?q=zzzznomatch")
check("search misses", len(r.get_json()["leads"]), 0)

r = c.patch(f"/api/leads/{lead_id}", json={"company": "Bernoulli Ltd"})
check("patch lead", r.get_json()["company"], "Bernoulli Ltd")

print("\n=== CSV import ===")
csv_text = """Full Name,Mobile,Company,Email
Grace Hopper,555-222-3333,Navy,grace@example.com
Alan Turing,(555) 444-5555,GCHQ,alan@example.com
Broken Row,not-a-number,X,x@example.com
Ada Lovelace,5551234567,Dup Co,ada@example.com
"""
r = c.post("/api/leads/import", json={"csv": csv_text})
rep = r.get_json()
check("import status", r.status_code, 200)
check("imported 2 new", rep["imported"], 2)
check("1 unparseable skipped", rep["skipped"], 1)
check("1 duplicate detected", rep["duplicates"], 1)
check("error detail given", len(rep["errors"]), 1)

# Header aliasing: totally different column names should still map.
r = c.post("/api/leads/import", json={
    "csv": "contact number,customer,organisation\n5559998888,Katherine Johnson,NASA\n"})
check("alias headers map", r.get_json()["imported"], 1)

r = c.post("/api/leads/import", json={"csv": "foo,bar\n1,2\n"})
check("missing phone column -> 400", r.status_code, 400)
check("helpful message", "phone column" in r.get_json()["error"], True)

print("\n=== Google Sheets URL parsing ===")
u = A.sheet_csv_url("https://docs.google.com/spreadsheets/d/ABC123_xyz/edit#gid=456")
check("sheet url -> csv export", u,
      "https://docs.google.com/spreadsheets/d/ABC123_xyz/export?format=csv&gid=456")
check("defaults to gid 0",
      A.sheet_csv_url("https://docs.google.com/spreadsheets/d/ABC123/edit"),
      "https://docs.google.com/spreadsheets/d/ABC123/export?format=csv&gid=0")
# SSRF guards
check("rejects other host", A.sheet_csv_url("https://evil.com/spreadsheets/d/ABC/edit"), None)
check("rejects http",  A.sheet_csv_url("http://docs.google.com/spreadsheets/d/ABC/edit"), None)
check("rejects metadata ip", A.sheet_csv_url("https://169.254.169.254/latest/meta-data"), None)
check("rejects non-sheet path", A.sheet_csv_url("https://docs.google.com/document/d/ABC"), None)
r = c.post("/api/leads/import", json={"sheet_url": "https://evil.com/x"})
check("bad sheet url -> 400", r.status_code, 400)

print("\n=== campaigns ===")
all_leads = c.get("/api/leads?limit=100").get_json()["leads"]
ids = [l["id"] for l in all_leads]
r = c.post("/api/campaigns", json={"name": "Q3 Outreach", "lead_ids": ids,
                                   "gap_seconds": 3, "max_attempts": 2,
                                   "retry_no_answer": True})
check("create campaign", r.status_code, 201)
camp = r.get_json()
cid = camp["id"]
check("campaign totals", camp["total"], len(ids))
check("campaign starts undone", camp["done"], 0)

q = c.get(f"/api/campaigns/{cid}/queue").get_json()
check("queue length", len(q), len(ids))
check("queue joins lead name", any(x["name"] == "Ada Lovelace" for x in q), True)

print("\n=== call linking + disposition ===")
c.post("/api/calls/link", json={"call_sid": "CA_test_1", "lead_id": lead_id,
                                "campaign_id": cid, "to": "+15551234567"})
q = c.get(f"/api/campaigns/{cid}/queue").get_json()
ada = [x for x in q if x["lead_id"] == lead_id][0]
check("attempt counted", ada["attempts"], 1)

r = c.post("/api/calls/CA_test_1/disposition",
           json={"disposition": "interested", "note": "Wants a demo Tuesday"})
check("set disposition", r.status_code, 200)

lead_now = c.get("/api/leads?q=Ada").get_json()["leads"][0]
check("lead status updated", lead_now["status"], "interested")
check("note appended to lead", "demo Tuesday" in lead_now["notes"], True)

q = c.get(f"/api/campaigns/{cid}/queue").get_json()
ada = [x for x in q if x["lead_id"] == lead_id][0]
check("campaign row done", ada["done"], 1)
check("campaign row disposition", ada["disposition"], "interested")

r = c.post("/api/calls/CA_test_1/disposition", json={"disposition": "bogus"})
check("unknown disposition rejected", r.status_code, 400)

print("\n=== DNC blocks dialing ===")
grace = [l for l in c.get("/api/leads?q=Grace").get_json()["leads"]][0]
c.post("/api/calls/link", json={"call_sid": "CA_test_2", "lead_id": grace["id"],
                                "to": grace["phone"]})
c.post("/api/calls/CA_test_2/disposition", json={"disposition": "dnc"})
grace_now = c.get("/api/leads?q=Grace").get_json()["leads"][0]
check("lead marked dnc", grace_now["dnc"], True)

# The whole point: a signed, allowlisted dial must still be refused.
r = signed("/connect", {"To": grace["phone"]})
body = r.get_data(as_text=True)
check("dnc number not dialed", "<Dial" in body, False)
check("dnc spoken refusal", "do not call" in body, True)

# A non-DNC lead still dials fine.
r = signed("/connect", {"To": "+15554445555"})
check("normal number still dials", "<Dial" in r.get_data(as_text=True), True)

# DNC leads are excluded when building a campaign from all leads.
r = c.post("/api/campaigns", json={"name": "All", "all_leads": True})
check("all_leads skips dnc", r.get_json()["total"], len(ids) - 1)

print("\n=== campaign progress from call status ===")
alan = [l for l in c.get("/api/leads?q=Alan").get_json()["leads"]][0]
c.post("/api/calls/link", json={"call_sid": "CA_test_3", "lead_id": alan["id"],
                                "campaign_id": cid, "to": alan["phone"]})
signed("/call_status", {"CallSid": "CA_test_3", "CallStatus": "no-answer",
                        "To": alan["phone"], "From": "+15550000000"})
q = c.get(f"/api/campaigns/{cid}/queue").get_json()
al = [x for x in q if x["lead_id"] == alan["id"]][0]
check("no-answer recorded", al["last_status"], "no-answer")
# retry_no_answer=True, max_attempts=2, attempts=1 -> stays open for a retry
check("retryable stays open", al["done"], 0)

signed("/call_status", {"CallSid": "CA_test_3", "CallStatus": "completed",
                        "To": alan["phone"], "From": "+15550000000",
                        "CallDuration": "95"})
q = c.get(f"/api/campaigns/{cid}/queue").get_json()
al = [x for x in q if x["lead_id"] == alan["id"]][0]
check("completed closes row", al["done"], 1)

print("\n=== reset ===")
c.post(f"/api/campaigns/{cid}/reset")
q = c.get(f"/api/campaigns/{cid}/queue").get_json()
check("reset clears done", sum(x["done"] for x in q), 0)
check("reset clears attempts", sum(x["attempts"] for x in q), 0)

print("\n=== analytics ===")
a = c.get("/api/analytics").get_json()
check("analytics 200", c.get("/api/analytics").status_code, 200)
check("counts calls", a["calls"] >= 3, True)
check("talk seconds from duration", a["talk_seconds"] >= 95, True)
check("connect rate is a number", isinstance(a["connect_rate"], float), True)
check("dispositions breakdown present", len(a["by_disposition"]) >= 1, True)
check("leads counted", a["leads_total"] >= 4, True)
check("dnc counted", a["leads_dnc"], 1)

print("\n=== delete ===")
check("delete lead", c.delete(f"/api/leads/{alan['id']}").status_code, 204)
check("delete campaign", c.delete(f"/api/campaigns/{cid}").status_code, 204)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
raise SystemExit(1 if fails else 0)
