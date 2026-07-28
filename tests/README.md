# Tests

```bash
npm --prefix tests install     # once: installs playwright
npx --prefix tests playwright install chromium
./tests/run_all.sh
```

Five suites:

| Suite | Covers |
|---|---|
| `test_security.py` | Twilio webhook signature validation, E.164 normalization, allow/block prefixes, DNC enforcement |
| `test_crm.py` | Leads CRUD, CSV + Google Sheets import, campaigns, dispositions, analytics |
| `test_wipe.py` | Bulk delete: confirmation, DNC preservation, call-history retention |
| `ui_test.mjs` | Keypad, long-press, in-call controls, DTMF, incoming call overlay |
| `ui_crm.mjs` | Import flow, lead detail, campaign queue, disposition sheet, analytics |

The runner uses port **5099** and `/tmp/suite.db`, so a dev server on 5055 with
your real `.env` is never disturbed. Twilio credentials in the suites are fake —
nothing contacts Twilio and no calls are placed.
