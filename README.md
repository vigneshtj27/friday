# friday

A self-hosted browser agent that operates a real Google Chrome on **your own** online accounts (banks, cards, utilities, shopping) and finishes tasks end to end. The first capability is *fetch statement*. You say what you want; it signs in (reusing the saved session when it can), handles one-time codes, downloads and checks the file, and only pings you when a human is genuinely needed.

It is a personal, single-user tool. Every account it touches is yours, you supply the credentials, and it runs only for you.

> **Respect each site's terms.** Some sites restrict automated access. Where a site offers an official export, an API, or e-statements by email, prefer those. The agent is told to look for them and records them in the site notes.

## How it works

```
 you ──► portal / CLI ──► job queue (one worker per account, one login per site)
                              │
                              ▼
               ┌──── login engine (state machine) ────┐
               │  classify page after every step:      │
               │  LOGGED_IN · OTP email/SMS/TOTP · push │
               │  security question · CAPTCHA · rejected│
               │  silent bounce · blocked · failed      │
               └──────────────┬────────────────────────┘
                              ▼
     agent loop (Claude, tools: goto click type press select scroll wait_for
                 read_page screenshot download_file extract_table
                 request_otp request_human request_approval finish)
                              │  every action ─► policy (allow / ask owner / deny)
                              ▼
     real Google Chrome, headed, on Xvfb, persistent profile per (site, account)
```

* **No per-site code.** One generic loop (observe, decide, act, verify) drives every site. What it learns about a site is written as short *notes* the agent reads next time. Notes are hints, never scripts, and are marked stale when a run contradicts them.
* **Real Chrome, persistent identity.** One fixed `user_data_dir` per (site, account), on an encrypted volume. Cookies and device-trust tokens survive between runs, so later logins look like a returning device. The browser's identity (version, UA, locale, timezone, screen) is recorded on first use, and drift is flagged.
* **Read-only by default, enforced in code.** Every interactive tool call is classified. Submits are allowed only in sign-in, filter and search forms. Anything that could pay, transfer, add a payee, change a setting, accept terms or send data waits for **your approval of that one action**, with a masked screenshot of the page.
* **Secrets never reach the model.** The model types `secret:<name>`; the injector fills the real value at the last moment, only into a field whose own frame is on the site's registered domains over HTTPS, and only for the account being driven. Password and code fields are blacked out in screenshots and hidden in page snapshots. Values are redacted from logs, the audit log, run steps and notifications.
* **Diagnose, don't assume.** A silent bounce or a block triggers a diagnosis report. It captures evidence, checks the browser fingerprint and the egress IP, runs one-variable page-load experiments, and ranks the likely causes. Anything not isolated by an experiment is marked *unconfirmed*. It also suggests the cheapest next experiment, usually "sign in by hand in the same profile".
* **No CAPTCHA solving, no evasion arms race.** CAPTCHAs and unknown challenges are handed to you through the remote view.

## Quick start (local, macOS or Linux desktop)

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Google Chrome, an Anthropic API key.

```bash
uv sync
cp env.example .env
```

```bash
uv run friday keygen --keyring
```

```bash
uv run friday hash-password
```

Put the printed `FRIDAY_ADMIN_PASSWORD_HASH='…'` line in `.env`, and set `ANTHROPIC_API_KEY` in your environment. On macOS, keep `FRIDAY_DATA_DIR` on your FileVault-encrypted disk (the default `./data` is fine if FileVault is on).

### Try it against the built-in mock bank first

```bash
uv run friday mock-site --port 8780
```

In another terminal:

```bash
uv run friday site add mockbank --domain localhost --login-url http://localhost:8780/s/sms_otp/login
uv run friday profile create mockbank alice
uv run friday secret set mockbank/alice/username --kind username
uv run friday secret set mockbank/alice/password --kind password
uv run friday serve
```

The test credentials are `alice` / `correct-horse-battery`. Open http://127.0.0.1:8765, log in, and press **Fetch statement** on the account. The mock "bank" texts a code: it appears in the portal's **Codes** tab (and in your chat app if you set one up). Try the other scenarios too: `email_otp`, `totp`, `push`, `bounce`, `denied`, `captcha`.

### Add a real account

1. **Register the site** with every domain the login touches (main site, auth/SSO domain, OTP domain):
   ```bash
   uv run friday site add mybank --name "My Bank" --login-url https://netbanking.mybank.example/login \
     --domain mybank.example --otp-domain auth.mybank.example --otp-sender alerts@mybank.example
   ```
2. **Create the profile** with the timezone and locale your egress IP implies:
   ```bash
   uv run friday profile create mybank me --timezone Asia/Kolkata --locale en-IN
   ```
3. **Store credentials** (prompted, never echoed):
   ```bash
   uv run friday secret set mybank/me/username --kind username
   ```
   ```bash
   uv run friday secret set mybank/me/password --kind password
   ```
   Optional: `mybank/me/totp-seed` (base32 or `otpauth://` URI), `mybank/me/security-answer-<topic>`, `mybank/me/statement-password`.
4. **Seed the profile by signing in once yourself.** This is the most important step: it makes later automated logins look like a returning, trusted device.
   - Locally: `uv run friday profile seed mybank.me` opens Chrome on that profile. Sign in (tick "remember this device" if offered), then press Enter in the terminal.
   - On a server: in the portal, press **Manual login** on the account. A remote-view tab opens on the same browser; sign in, then click **Done**.
5. **Run it**: portal → **Fetch statement**, or
   ```bash
   uv run friday run fetch_statement mybank.me --start 2026-09-01 --end 2026-09-30
   ```

## One-time codes

| Channel | How it works |
|---|---|
| Email | Connect Gmail once: create a Google Cloud OAuth client of type *Desktop app*, set `FRIDAY_GOOGLE_CLIENT_ID`/`_SECRET`, run `uv run friday gmail connect`. Scope is `gmail.readonly`. The agent polls for a message from the site's `--otp-sender` that arrived after it submitted, extracts the code and discards the email. It times out after about 90 s, then asks you. |
| SMS | You get a message: "Enter the SMS code for My Bank". Reply with the code in Telegram / ntfy / WhatsApp, or type it in the portal's **Codes** tab (with a countdown). Optional automatic path: point an Android SMS-forwarder app at `POST /hooks/sms` with header `X-Friday-Token: $FRIDAY_SMS_WEBHOOK_TOKEN`. |
| TOTP | Store the seed as `<site>/<account>/totp-seed`; codes are generated locally. |
| Push approval | You are notified; the agent watches the page until it moves on (3 min). |

Codes are held in memory as single-use secrets, never logged and never reused.

## Notifications

`FRIDAY_NOTIFY=telegram` (or `ntfy`, `whatsapp`, comma-separated). See `env.example` for each channel's settings. Messages say what is needed and link to the portal or the remote view; secret values are redacted from every outgoing message. Inbound replies are accepted only from the configured chat, topic or number, and they only resolve a pending code request.

## The portal

`http://127.0.0.1:8765/portal` (password login, single user):

- **Accounts**: session status per account, run login / fetch statement, manual login, **Revoke everything** (wipes the Chrome profile and all of that account's secrets).
- **Runs**: live step log with masked screenshots, cost, stop button.
- **Approvals**: exactly what will happen, with a screenshot, approve or deny this one action.
- **Codes**: pending OTP requests with a countdown.
- **Handoffs**: links to the remote view of the exact browser session.
- **Diagnosis**: ranked causes, evidence, experiments, buttons for the manual-login test and probes.
- **Statements**: downloaded files (decrypted on download) and normalized transactions, with CSV export.
- **Secrets** (names only), **Sites** (domains, OTP senders, site notes, backoff), **Audit** (hash-chained log of every action and every secret use, by name).

## Deploying

### Docker (home machine or VPS)

```bash
cp env.example .env
```

Fill in `FRIDAY_MASTER_KEY` (from `uv run friday keygen`), `FRIDAY_ADMIN_PASSWORD_HASH`, `ANTHROPIC_API_KEY`, `FRIDAY_PUBLIC_BASE_URL` (your VPN address) and the notification settings. Put `FRIDAY_HOST_DATA` on an encrypted disk. Then:

```bash
docker compose up -d --build
```

The image contains Google Chrome (amd64; it runs under emulation on Apple Silicon, slowly), Xvfb, x11vnc, noVNC and the app. The port is published on `127.0.0.1` only. Reach it over Tailscale/WireGuard or an SSH tunnel, never the open internet. Chrome's sandbox stays on via `docker/chrome-seccomp.json` (Docker's default profile plus three namespace syscalls), with all capabilities dropped.

For the first-run profile seeding on a server, use **Manual login** in the portal: the remote view shows the server's Chrome in your browser.

### Home machine vs VPS: the IP-reputation tradeoff

Banks score logins by network as well as by browser. A **VPS** has a datacenter IP: often flagged as hosting, sometimes in another country, shared with other customers' traffic. That means more "unusual activity" blocks, silent bounces and extra OTPs. A **home machine** has your usual residential IP and timezone, which is exactly what the bank has seen for years.

Options, best first:

1. Run friday at home (an always-on mini PC or NAS with Docker).
2. Run friday on a VPS but send the browser's traffic out through home: run `friday egress-proxy --listen <home-vpn-ip>:3128` on a home machine (password in `FRIDAY_EGRESS_PASSWORD`), store the proxy username/password in the vault, and set the profile's proxy to `http://<home-vpn-ip>:3128`. Set the profile timezone to match home.
3. A static residential proxy you trust. Keep one proxy per profile, and never rotate.

Whatever you choose, keep it **stable**: the same egress, timezone and locale for a profile, every time. The diagnosis report flags a datacenter IP or a timezone/IP mismatch when it sees one.

## Safety model, in one place

| Rule | Where it is enforced |
|---|---|
| Only your registered domains | `goto` and link policy, plus a network-level block on any main-frame navigation elsewhere |
| Never type a secret anywhere else | Injector checks the profile scope, the field's frame domain and HTTPS |
| Read-only by default | Policy plus approval gate on every risky action, per action, timing out to "deny" |
| Page text is untrusted | Wrapped and labelled for the model; operator messages use a separate system channel; approvals happen only in the portal |
| No lockouts | A rejected password gets one retry; no state may repeat more than twice; exponential backoff per site |
| No secrets in logs | Redactor on logs, audit, run steps, notes, notifications; tracebacks never render locals; Playwright debug logging is refused |
| Data at rest | Secrets and files encrypted (XChaCha20-Poly1305); profiles must be on an encrypted volume |

## Development

```bash
uv run pytest
```

```bash
uv run pytest -m chrome
```

The first runs the unit tests: classifier, OTP extraction, TOTP, approval gate, policy, secret masking, crypto, dedupe/verification, diagnosis ranking, web auth and the VNC bridge. The second launches real Chrome against the mock bank through the full job runner, for every login scenario plus statement fetch, the approval gate and the navigation block. It uses a heuristic stand-in for the model, so it needs no API key.

See `docs/decisions.md` for the design choices and `docs/status.md` for what is verified and what is not.
