# Tests

```bash
npm --prefix tests install     # once: installs playwright
npx --prefix tests playwright install chromium
./tests/run_all.sh
```

Seven suites:

| Suite | Covers |
|---|---|
| `test_security.py` | Twilio webhook signature validation, E.164 normalization, allow/block prefixes, DNC enforcement |
| `test_crm.py` | Leads CRUD, CSV + Google Sheets import, campaigns, dispositions, analytics |
| `test_wipe.py` | Bulk delete: confirmation, DNC preservation, call-history retention |
| `test_extras.py` | Recording + AMD TwiML, recordings API and audio proxy, area-code and country-code timezones, the calling window, follow-up tasks, templates and merge fields, the one-segment SMS cap, delivery receipts, per-call/per-message cost sync, inbound SMS opt-out, CSV export, search, analytics windows/filters/comparisons |
| `ui_test.mjs` | Keypad, long-press, callee local-time clock, in-call controls, DTMF, incoming call overlay |
| `ui_crm.mjs` | Import flow, lead detail, campaign queue, disposition sheet, analytics sections and range/campaign filters |
| `ui_extras.mjs` | Follow-ups view, templates and the composer picker, SMS character counters, recordings view, lead timeline and enrichment badges, CSV download |

`test_extras.py` and `ui_extras.mjs` run against a server with `RECORD_CALLS`,
`AMD_ENABLED` and the voicemail message switched on, so the configured paths are
what gets asserted. The other suites keep running against the defaults.

The runner uses port **5099** and `/tmp/suite.db`, so a dev server on 5055 with
your real `.env` is never disturbed. Twilio credentials in the suites are fake —
nothing contacts Twilio and no calls are placed.
