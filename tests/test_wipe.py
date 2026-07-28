import os
os.environ.update({
 "TWILIO_ACCOUNT_SID":"AC"+"0"*32,"TWILIO_AUTH_TOKEN":"test_auth_token_12345",
 "TWILIO_API_KEY":"SK"+"0"*32,"TWILIO_API_SECRET":"s","TWILIO_APP_SID":"AP"+"0"*32,
 "TWILIO_PHONE_NUMBER":"+15550000000","PUBLIC_BASE_URL":"https://d.example.com",
 "DB_PATH":"/tmp/test_wipe.db"})
import app as A
fails=[]
def check(l,g,w):
    ok=g==w; print(f"{'PASS' if ok else 'FAIL'}  {l}: got={g!r}")
    if not ok: fails.append(l); print(f"      wanted {w!r}")

c=A.app.test_client()

# seed
c.post("/api/leads/import", json={"csv":"name,phone\nA,5551110001\nB,5551110002\nC,5551110003\nD,5551110004\n"})
leads=c.get("/api/leads?limit=100").get_json()["leads"]
check("seeded 4", len(leads), 4)

# mark one DNC, attach a call to it
dnc_lead = leads[0]
c.patch(f"/api/leads/{dnc_lead['id']}", json={"dnc": True})
c.post("/api/calls/link", json={"call_sid":"CA_wipe_1","lead_id":leads[1]["id"],"to":leads[1]["phone"]})
# put everything in a campaign
camp=c.post("/api/campaigns", json={"name":"W","lead_ids":[l["id"] for l in leads]}).get_json()
check("campaign has 4", camp["total"], 4)

print("\n=== confirmation is required ===")
check("no confirm -> 400", c.delete("/api/leads").status_code, 400)
check("wrong confirm -> 400", c.delete("/api/leads", json={"confirm":"yes"}).status_code, 400)
check("still 4 leads", len(c.get("/api/leads?limit=100").get_json()["leads"]), 4)

print("\n=== delete all, keeping DNC (default) ===")
r=c.delete("/api/leads", json={"confirm":"DELETE"})
rep=r.get_json()
check("status 200", r.status_code, 200)
check("deleted 3", rep["deleted"], 3)
check("kept 1 dnc", rep["kept_dnc"], 1)

remaining=c.get("/api/leads?limit=100").get_json()["leads"]
check("1 lead remains", len(remaining), 1)
check("the survivor is the DNC one", remaining[0]["dnc"], True)

print("\n=== call history survives, lead link detached ===")
import sqlite3
conn=sqlite3.connect("/tmp/test_wipe.db"); conn.row_factory=sqlite3.Row
row=conn.execute("SELECT lead_id FROM call_log WHERE call_sid='CA_wipe_1'").fetchone()
check("call row still exists", row is not None, True)
check("lead_id nulled, not cascaded", row["lead_id"], None)

print("\n=== campaign membership cleaned ===")
q=c.get(f"/api/campaigns/{camp['id']}/queue").get_json()
check("queue only has the DNC lead", len(q), 1)

print("\n=== delete including DNC ===")
r=c.delete("/api/leads", json={"confirm":"DELETE","keep_dnc":False})
check("deleted the last one", r.get_json()["deleted"], 1)
check("no leads left", len(c.get("/api/leads?limit=100").get_json()["leads"]), 0)
check("empty delete is a no-op", c.delete("/api/leads", json={"confirm":"DELETE"}).get_json()["deleted"], 0)

print("\n"+("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
raise SystemExit(1 if fails else 0)
