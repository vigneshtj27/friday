# Status: what is verified and what is not

Last updated 2026-10-16.

## Verified

| What | How | Where |
|---|---|---|
| Unit tests (classifier, OTP extraction, TOTP, approval gate, policy, masking, crypto, dedupe and verification, diagnosis ranking, web auth, VNC bridge, injector) | `uv run pytest`: 151 pass | Linux and macOS |
| The exact request sent to the Anthropic API | `tests/test_llm_wire.py`: real SDK against a local fake API | Linux and macOS |
| Persistent profile in a real browser (cookie survives a restart, secret injection, masked screenshot, pinned fingerprint) | `tests/test_chrome_smoke.py`, both `playwright` and `cdp` drivers | macOS with Google Chrome; Linux Xvfb with Playwright's bundled Chromium 141 |
| Every login scenario against the mock bank (session reuse, email OTP via Gmail stand-in, SMS OTP by owner reply, TOTP, push, wrong password, silent bounce plus diagnosis, access denied, CAPTCHA handoff), statement fetch with verification and dedupe, payment approval gate, navigation allowlist | `tests/test_e2e_mockbank.py`: 12 pass, using a heuristic stand-in for the model | Linux Xvfb with bundled Chromium |
| Docker image: seeding a profile, a second seed of the same profile refused, remote-view login and handoff round trip, no fingerprint warnings | Manual | Docker Desktop on Apple Silicon (x86 image under emulation) |

How to run the real-browser tests on Linux without Google Chrome installed:

```bash
FRIDAY_CHROME_EXECUTABLE=/path/to/chrome uv run pytest -m chrome
```

They need `Xvfb` and `x11vnc` on PATH. When running as root (as in some CI containers), Chrome refuses to start sandboxed, so add `FRIDAY_CHROME_SANDBOX=false`. Use that only for tests: the Docker image runs as a non-root user with the sandbox on.

## Not verified

- **Real sites.** No real bank, card, utility or shopping site has been driven end to end. The first run on each site is expected to need a manual sign-in (seed) and may need a diagnosis.
- **The real model driving the loop.** The end-to-end tests use a heuristic stand-in. Prompts and tool definitions are exercised only at the wire level.
- **Gmail OAuth against Google.** The PKCE flow and message matching are tested against a fake. The consent screen and refresh-token exchange have not been run against a real Google project.
- **Telegram, WhatsApp, ntfy and SMS-forwarder delivery.** Message formatting and reply acceptance are unit-tested; live delivery is not.
- **`patchright` driver.** Optional extra, not installed in the test runs.
- **Image-only (scanned) PDF statements.** Not supported; see `decisions.md`.

## Fixed in this pass

- The Playwright driver ignored `FRIDAY_CHROME_EXECUTABLE` and always looked for Chrome at its default install path. It now uses the configured executable, except when a diagnosis experiment asks for a specific channel.
- The display backend was resolved once, when the app started, so changing `display_backend` later had no effect. It is now read on each use.
- Under the `cdp` driver, Chrome dropped the first 250–400 ms of keystrokes after a page committed, so a typed password lost its first characters. That would have spent a real login attempt with a wrong password. Typing now reads the field back, retypes once on a mismatch, then sets the value directly. A secret that still doesn't match is cleared, and the agent is told nothing was submitted.
