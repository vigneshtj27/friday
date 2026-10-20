# Status: what is verified and what is not

Last updated 2026-10-20.

## Verified

| What | How | Where |
|---|---|---|
| Unit tests (classifier, OTP extraction, TOTP, approval gate, policy, masking, crypto, dedupe and verification, diagnosis ranking, web auth, VNC bridge, injector) | `uv run pytest`: 204 pass | Linux and macOS |
| The exact request sent to the Anthropic API | `tests/test_llm_wire.py`: real SDK against a local fake API | Linux and macOS |
| Persistent profile in a real browser (cookie survives a restart, secret injection, masked screenshot, pinned fingerprint) | `tests/test_chrome_smoke.py`, both `playwright` and `cdp` drivers | macOS with Google Chrome; Linux Xvfb with Playwright's bundled Chromium 141 |
| Every login scenario against the mock bank (session reuse, email OTP via Gmail stand-in, SMS OTP by owner reply, TOTP, push, wrong password, silent bounce plus diagnosis, access denied, CAPTCHA handoff), statement fetch with verification and dedupe, payment approval gate, navigation allowlist | `tests/test_e2e_mockbank.py`: 12 pass, using a heuristic stand-in for the model | Linux Xvfb with bundled Chromium |
| Built-in chat: code card resolves the waiting sign-in, approve button clears the gate, cards update in place, phone width | `tests/test_chat.py` plus a scripted browser run against the live server | Linux, bundled Chromium |
| Chat assistant: free text starts runs, codes bypass the model, tool errors returned to the model, transaction search and totals, typing indicator, live run cards | `tests/test_assistant.py` (scripted model) plus a scripted browser run | Linux, bundled Chromium |
| Activity fetch: ARIA grids and plain tables read by `extract_table`, year-less dates, card sign convention (minus = refund, CR = payment), columns inferred when there's no header, reported rows kept only if they appear in page text, range filter, verify, dedupe | `tests/test_activity.py` (rows, plus the fixture HTML through the real `extract_table` JavaScript in Chromium) | Linux, bundled Chromium |
| Sheets writer: column matching, blank note/category columns, `INSERT_ROWS` append with existing rows unchanged, duplicates skipped on re-run, formula-looking text escaped, read-back row count and total, quoted tab names; Google OAuth (single-use state, partial grant refused, token in `system/` vault namespace) | `tests/test_sheets.py` against a mocked Google API | Linux |
| "Fetch and populate my Oct <card> expenses": saved sheet asked for and checked, card statement request for recent days switched to an activity fetch carrying the sheet step | `tests/test_assistant.py` (scripted model) | Linux |
| `patchright` driver hides `navigator.webdriver` (plain Playwright shows it) and the profile locale is applied consistently | `tests/test_chrome_smoke.py::test_driver_webdriver_flag_and_locale`, now against a local page (no network) and honouring `FRIDAY_CHROME_EXECUTABLE`; run under `xvfb-run -a` on a headless box | Linux, bundled Chromium 141 |
| Docker image: seeding a profile, a second seed of the same profile refused, remote-view login and handoff round trip, no fingerprint warnings | Manual | Docker Desktop on Apple Silicon (x86 image under emulation) |

How to run the real-browser tests on Linux without Google Chrome installed:

```bash
FRIDAY_CHROME_EXECUTABLE=/path/to/chrome uv run pytest -m chrome
```

They need `Xvfb` and `x11vnc` on PATH. When running as root (as in some CI containers), Chrome refuses to start sandboxed, so add `FRIDAY_CHROME_SANDBOX=false`. Use that only for tests: the Docker image runs as a non-root user with the sandbox on.

## Not verified

- **Real sites.** No real bank, card, utility or shopping site has been driven end to end. The first run on each site is expected to need a manual sign-in (seed) and may need a diagnosis.
- **Amex end to end.** Not run: the live profile, home egress, Gmail connection and re-seed are owner steps on the deployment (see `runbook.md`); this environment has no access to it. The activity fetch has been tested on fixture pages only, not on a real activity page.
- **Google Sheets and Gmail against Google.** The OAuth flow and the Sheets calls are tested against mocks. Real consent, real API responses (formatting of dates/numbers in your sheet's locale) are not yet verified; the read-back check is there to catch a mismatch on the first real run.
- **The real model in the chat.** The assistant's tool use is tested with a scripted model only; no request has gone to the API from the chat yet.
- **`task` runs on a real site.** The capability reuses the tested agent loop, but no open-ended task has been run end to end.
- **The real model driving the loop.** The end-to-end tests use a heuristic stand-in. Prompts and tool definitions are exercised only at the wire level.
- **Gmail OAuth against Google.** The PKCE flow and message matching are tested against a fake. The consent screen and refresh-token exchange have not been run against a real Google project.
- **Telegram, WhatsApp, ntfy and SMS-forwarder delivery.** Message formatting and reply acceptance are unit-tested; live delivery is not.
- **Image-only (scanned) PDF statements.** Not supported; see `decisions.md`.

## Fixed in this pass

- The Playwright driver ignored `FRIDAY_CHROME_EXECUTABLE` and always looked for Chrome at its default install path. It now uses the configured executable, except when a diagnosis experiment asks for a specific channel.
- The display backend was resolved once, when the app started, so changing `display_backend` later had no effect. It is now read on each use.
- Under the `cdp` driver, Chrome dropped the first 250–400 ms of keystrokes after a page committed, so a typed password lost its first characters. That would have spent a real login attempt with a wrong password. Typing now reads the field back, retypes once on a mismatch, then sets the value directly. A secret that still doesn't match is cleared, and the agent is told nothing was submitted.
- A sign-in form sent by script (fetch/XHR, then a redirect, as American Express does) was classified while its button was still spinning. Playwright's load-state waits return at once when nothing navigates, so the page still showed the login form and the run stopped as `SILENT_BOUNCE`. A submitting click or Enter now waits (up to 20 s) until the page navigates or every request started by the submit has finished. A bounce verdict is re-checked 4 s later before the run stops. Mock bank scenario `xhr` covers both.
