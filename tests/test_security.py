"""Exercise the security-critical paths of app.py end-to-end."""
import os

os.environ.update({
    "TWILIO_ACCOUNT_SID":  "AC" + "0" * 32,
    "TWILIO_AUTH_TOKEN":   "test_auth_token_12345",
    "TWILIO_API_KEY":      "SK" + "0" * 32,
    "TWILIO_API_SECRET":   "secret",
    "TWILIO_APP_SID":      "AP" + "0" * 32,
    "TWILIO_PHONE_NUMBER": "+15550000000",
    "PUBLIC_BASE_URL":     "https://dialer.example.com",
    "DB_PATH":             "/tmp/test_dialer_sec.db",
})

import app as A
from twilio.request_validator import RequestValidator

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")
    if not ok:
        fails.append(label)


print("=== normalize_number ===")
check("10-digit US",      A.normalize_number("5551234567"),      "+15551234567")
check("formatted US",     A.normalize_number("(555) 123-4567"),  "+15551234567")
check("11-digit w/ 1",    A.normalize_number("1-555-123-4567"),  "+15551234567")
check("already E.164",    A.normalize_number("+15551234567"),    "+15551234567")
check("UK E.164",         A.normalize_number("+442071234567"),   "+442071234567")
check("client identity",  A.normalize_number("client:bob"),      "client:bob")
check("empty",            A.normalize_number(""),                None)
check("junk",             A.normalize_number("abcdef"),          None)
check("too short",        A.normalize_number("12345"),           None)
check("9-digit too short",A.normalize_number("123456789"),      None)
check("bare India CC",   A.normalize_number("919876543210"),   "+919876543210")
check("bare UK CC",      A.normalize_number("442071234567"),   "+442071234567")
check("bare AU CC",      A.normalize_number("61412345678"),    "+61412345678")
check("spaced India",    A.normalize_number("91 98765 43210"), "+919876543210")
check("too long",         A.normalize_number("+1234567890123456"), None)

print("\n=== is_dialable: all countries allowed (ALLOWED_PREFIXES=*) ===")
check("US allowed",        A.is_dialable("+15551234567"),   True)
check("UK allowed",        A.is_dialable("+442071234567"),  True)
check("India allowed",     A.is_dialable("+919876543210"),  True)
check("Australia allowed", A.is_dialable("+61412345678"),   True)
# The blocklist must still bite even when every country is permitted.
check("premium 900 blocked",   A.is_dialable("+19005551234"), False)
check("976 blocked",           A.is_dialable("+19765551234"), False)
check("Caribbean 809 blocked", A.is_dialable("+18095551234"), False)
check("None",                  A.is_dialable(None),           False)

print("\n=== is_dialable: restricted mode still works ===")
_saved_all, _saved_pref = A.ALLOW_ALL_COUNTRIES, A.ALLOWED_PREFIXES
A.ALLOW_ALL_COUNTRIES, A.ALLOWED_PREFIXES = False, ("+1",)
check("US allowed when restricted",  A.is_dialable("+15551234567"),  True)
check("UK refused when restricted",  A.is_dialable("+442071234567"), False)
A.ALLOW_ALL_COUNTRIES, A.ALLOWED_PREFIXES = _saved_all, _saved_pref

print("\n=== webhook signature validation ===")
client = A.app.test_client()

# 1. Unsigned request must be refused.
r = client.post("/connect", data={"To": "+19005551234"})
check("unsigned /connect -> 403", r.status_code, 403)

r = client.post("/incoming", data={"From": "+15551234567", "CallSid": "CA1"})
check("unsigned /incoming -> 403", r.status_code, 403)

r = client.post("/call_status", data={"CallSid": "CA1"})
check("unsigned /call_status -> 403", r.status_code, 403)

# 2. Forged signature must be refused.
r = client.post("/connect", data={"To": "+15551234567"},
                headers={"X-Twilio-Signature": "bogussignature=="})
check("forged signature -> 403", r.status_code, 403)


def signed(path, params):
    """Build a request Twilio itself would have signed."""
    url = "https://dialer.example.com" + path
    sig = RequestValidator("test_auth_token_12345").compute_signature(url, params)
    return client.post(path, data=params, headers={"X-Twilio-Signature": sig})


# 3. Correctly signed request is accepted and dials.
r = signed("/connect", {"To": "5551234567"})
body = r.get_data(as_text=True)
check("signed /connect -> 200", r.status_code, 200)
check("signed /connect dials E.164", "<Number" in body and "+15551234567" in body, True)

# 4. Signed but disallowed destination is still refused at the allowlist.
r = signed("/connect", {"To": "+19005551234"})
body = r.get_data(as_text=True)
check("signed premium-rate -> no Dial", "<Dial" in body, False)
check("signed premium-rate -> spoken refusal", "not permitted" in body, True)

# 5. Signed inbound routes to the browser client.
r = signed("/incoming", {"From": "+15551234567", "CallSid": "CAtest1"})
body = r.get_data(as_text=True)
check("signed /incoming -> 200", r.status_code, 200)
check("/incoming dials client", "<Client>dialer-user</Client>" in body, True)

# 6. Status callback persists a disposition.
signed("/call_status", {
    "CallSid": "CAtest2", "CallStatus": "no-answer",
    "To": "+15551234567", "From": "+15550000000", "Direction": "outbound-dial",
})
r = client.get("/dispositions")
data = r.get_json()
check("/dispositions returns row", len(data) >= 1, True)
check("disposition mapped", data[0]["disposition"], "no-answer")

print("\n=== send_sms validation ===")
r = client.post("/send_sms", json={"to": "+19005551234", "body": "hi"})
check("premium-rate SMS -> 403", r.status_code, 403)
r = client.post("/send_sms", json={"to": "notanumber", "body": "hi"})
check("bad number SMS -> 400", r.status_code, 400)
r = client.post("/send_sms", json={"to": "+15551234567", "body": ""})
check("empty body SMS -> 400", r.status_code, 400)

print("\n=== token ===")
r = client.get("/token")
d = r.get_json()
check("token 200", r.status_code, 200)
check("token has identity", d.get("identity"), "dialer-user")
check("token has ttl", d.get("ttl"), 3600)

print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
raise SystemExit(1 if fails else 0)
