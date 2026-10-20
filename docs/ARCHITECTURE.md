# friday — architecture and evaluation guide

A complete description of the app for an independent reviewer (human or AI). It explains what friday
is, every subsystem and how they fit, the data and security model, how it is deployed, and an honest
list of limitations and things worth scrutinising. Paths like `friday/agent/loop.py` point at the
code so claims here can be checked against it.

> Companion documents: `README.md` (how to run it), `docs/decisions.md` (why each design choice was
> made), `docs/status.md` (what is verified vs unverified). This file is the single-source overview.

---

## 1. What friday is

friday is a **self-hosted, single-user personal assistant**. The owner gives it any task in the chat;
a **planner** breaks it into steps over a **tool layer** — a **real Google Chrome** on the owner's
*own* online accounts (banks, cards, utilities, shopping), the owner's Google Sheets, Gmail and
Drive, the web, stored files, long-term memory and schedules — runs them, **verifies** the result in
code, and reports back. The browser is one tool among several. Its built-in capabilities include
fetching a statement or a card's recent activity, which are parsed, verified and stored as
normalised transactions (see §16 for the assistant layer).

Design principles, enforced in code, not just prompts:

- **No per-site code.** One generic agent loop (observe → decide → act → verify) drives every site.
  What friday learns about a site is written as short free-text *notes* it reads next time; there are
  no per-bank selectors or scripted steps.
- **Real, persistent, consistent browser identity.** Real Chrome (`channel=chrome`), one persistent
  profile per `(site, account)`, stable fingerprint, optional stable egress IP. No per-run throwaway
  profiles, no per-request fingerprint randomisation.
- **Read-only by default.** Anything that spends money, changes a setting, adds a payee or sends data
  needs the owner's per-action approval. This is a code gate, not a prompt instruction.
- **Secrets never reach the model.** The LLM only ever handles references like
  `secret:amex/me/password`; the real value is injected into the page at the last moment by a
  component the model cannot call, and is redacted from everything that leaves the process.
- **The owner is only interrupted when a human is genuinely required** (a one-time code, an approval,
  a CAPTCHA, a first manual login). Everything else is autonomous.
- **Diagnose, don't guess.** A silent bounce or an edge block triggers an evidence-gathering
  diagnosis with one-variable experiments and a ranked, explicitly-unconfirmed-until-proven report.

It is deliberately *not* an evasion tool: no CAPTCHA solving, no IP rotation to dodge blocks, no
fingerprint spoofing. The only "stealth" is an opt-in, per-profile, audited driver (patchright) used
when a diagnosis shows a concrete need.

---

## 2. Technology and shape

- **Language/runtime:** Python 3.12+, asyncio throughout.
- **Web:** FastAPI + uvicorn. Two surfaces: a **chat** (the primary UI) and an **admin portal**
  (backup / inspection). Plain-JS front ends, no build step.
- **Browser automation:** Playwright driving real Chrome; pluggable drivers (see §6).
- **LLM:** Anthropic API (`claude-opus-5-5` default) via the official SDK, adaptive thinking,
  server-side refusal fallbacks, automatic prompt caching.
- **Storage:** SQLite (WAL). Secrets and downloaded files are encrypted at rest with libsodium
  (XChaCha20-Poly1305). Chrome profiles live on a volume the app requires to be encrypted.
- **Packaging:** one Docker image (Chrome + Xvfb + x11vnc + noVNC + the app). Deployed to a single
  GCE VM behind an HTTPS reverse proxy.
- **Size:** ~12.8k lines of app code across 79 modules; 246 unit tests + 21 real-Chrome tests (mock
  bank and fixture pages), all passing. File tree in §13.

Everything is one process: the FastAPI app, the job queue, the agent, and the browser sessions share
an event loop and an in-memory `AppContext` (`friday/context.py`) that wires every component together.

---

## 3. Request lifecycle (the 30-second mental model)

```
owner (chat)  "fetch and populate my Oct Amex expenses into the sheet"
  │  a code? → straight to the waiting sign-in (never to the model)
  ▼
Planner (friday/planner.py)   ── one call plans the steps: tool, args ($ref to earlier results),
  │                               progress line, success checks; memory + accounts in context
  ▼
ToolRegistry.call (friday/tools/base.py)   ── for EVERY step: validate args, gate (approval card
  │                                            for send/share/spend/publish and risky writes),
  │                                            owner-quote rule, audit, redact, mark untrusted
  ├─► browser_run ─► JobRunner ─► LoginEngine ─► AgentLoop ─► capability (fetch_activity,
  │                  (friday/jobs.py: per-profile worker,       fetch_statement, task): parse,
  │                   per-site lock, backoff; persistent         verify, dedupe → TransactionStore
  │                   Chrome profile; OTP, approvals,            (progress lines stream to the
  │                   handoffs, diagnosis as before)             plan card in the chat)
  ├─► sheets_* / gmail_* / drive_*  (owner's Google, one OAuth grant, token in the vault)
  ├─► web_search / web_fetch / files_read / table_compute / memory_* / schedule_*
  │   accounts, setup cards (onboarding: secrets go from a form straight to the vault)
  ▼
checks evaluated in code against each result; a failure → replan with the evidence (≤ 2),
then a blocker; a denied approval ends the plan
  ▼
final chat message: what was done + "Verified: …" (only code-checked) + "Not independently
verified: …"; the outcome is saved to memory
```

---

## 4. The secret vault and redaction (`friday/vault/`)

The core safety invariant: **the model never sees a secret, and no secret leaves the process.**

- **`crypto.py`** — XChaCha20-Poly1305 AEAD via PyNaCl. Random 192-bit nonce per ciphertext. The
  secret's *name* is bound as associated data, so a blob copied to another row won't decrypt.
  Purpose-specific subkeys (`secrets`, `files`, `web-session`) are derived from the master key with
  BLAKE2b. Master key: 32 bytes from env (`FRIDAY_MASTER_KEY`) or the OS keyring.
- **`store.py`** — `SecretStore`. Encrypted rows keyed by a path name (`site/account/kind`). The only
  method that returns a value is `reveal()`, which audits the use (by name), registers the value with
  the redactor, and hands it over. **Ephemeral secrets** (OTP codes) are held in memory only, are
  single-use (revealing pops them), and expire. Secret *names* are not sensitive and stay readable.
- **`injector.py`** — `SecretInjector`. The last-mile gate. Before typing a secret it checks: the
  secret belongs to the profile being driven (`scope.secret_prefix`), and the *field's own frame*
  (not just the tab) is on the site's registered domains over HTTPS (localhost exempt for the mock).
  It types key-by-key and reads the value back (Chrome launched without automation flags drops the
  first keystrokes after a page commits; a wrong secret is cleared rather than submitted). It also
  produces the **only screenshot path** that leaves the executor, painting over password fields and
  any field a secret was typed into.
- **`masking.py`** — `Redactor`. Every revealed value (and every OTP) is registered here, including
  URL-encoded variants. All logs, audit details, run-step text, site notes, notifications and
  model-facing text pass through it. A `logging.Filter` redacts messages *and* tracebacks; Typer's
  pretty-exceptions-with-locals is disabled so a traceback can't print a secret; Playwright protocol
  logging is refused at startup for the same reason.

**What to probe here:** can any code path serialise a profile/config/secret object with a live value
into a log, an API response, the audit log, or a model prompt? (Tests `tests/test_vault.py`,
`tests/test_injector.py` attack exactly this.)

---

## 5. Profiles, drivers and the browser (`friday/profiles/`, `friday/drivers/`)

- **`profiles/models.py`** — `Site` (id, name, login_url, domains, otp_domains, otp_senders) and
  `Profile` (`<site>.<account>`, `ProfileConfig` with locale/timezone/window/driver/proxy). Domains
  are validated; a login URL must sit inside the site's own domains (`site_problems()`), which is
  checked on every save and at login start so a bad record fails loudly instead of being silently
  blocked by the navigation allowlist.
- **`profiles/manager.py`** — `ProfileManager`. One persistent `user_data_dir` per `(site, account)`
  on the encrypted volume. **At most one open per profile** (an OS file lock, so it holds across
  processes too). On open it: checks the volume is encrypted (`volume.py`; refuse/warn configurable),
  acquires a display, launches the driver, and on the first non-experimental open **pins the browser
  fingerprint** (UA, locale, timezone, screen, webdriver flag…) and warns on later drift. `wipe()` is
  "revoke everything": delete the Chrome profile, downloads and all the account's secrets. It also
  supports one-variable **diagnosis overrides** (headless, no-proxy, alt-proxy, bundled Chromium,
  fresh throwaway profile) without disturbing the pinned identity.
- **`profiles/display.py`** — where the headed Chrome window lives. macOS: native window. Linux/Docker:
  a private **Xvfb** per open profile plus an **x11vnc** (localhost only, random per-session password,
  `-xkb` so punctuation/shift survive) for the remote-view handoff.
- **`drivers/`** — pluggable, one Playwright `BrowserContext` API regardless of launch method:
  - `playwright` — `launch_persistent_context`, real Chrome, Chrome's sandbox *on*, a consistent
    `locale` set on the context. No stealth. Default.
  - `patchright` — same API via the Patchright fork; **removes `navigator.webdriver`** and similar
    automation tells (verified in `tests/test_chrome_smoke.py`). Opt-in per profile, flagged
    `stealth=True`, and audited on every open. Used only on a diagnosed need.
  - `cdp` — launches Chrome as a plain process (no `--enable-automation`, none of Playwright's default
    flags) and attaches over CDP. The idea nodriver uses, without a second tool's API.
- **`agent/isolated.py`** — runs friday's own page scripts (the DOM snapshot walker, table extraction)
  in a **CDP isolated world**, not through the page's `eval`. Some banks (American Express) disable
  `window.eval`/`Function`; before this, every page read failed and the page looked blank. This is the
  fix for that class of "hardened page" (regression-tested with a mock scenario that disables `eval`).

**What to probe here:** profile isolation (never shared between accounts), the single-open lock under
concurrency, fingerprint consistency, and whether the diagnosis overrides can accidentally overwrite
the pinned identity.

---

## 6. The agent loop and the action policy (`friday/agent/`)

- **`snapshot.py`** — turns a live page into a compact, model-friendly view: interactive elements with
  stable `ref` ids (assigned across frames), visible text, form context (is this an auth / filter /
  search / other form?), captcha detection. Password fields and secret-filled fields are **never**
  shown with a value. The whole snapshot is redacted and wrapped as untrusted data.
- **`loop.py`** — `AgentLoop`. The generic observe→decide→act→verify loop. Appends history
  append-only (thinking blocks preserved), enforces hard **limits per task** (max steps, wall time,
  cost in USD), stops after too many consecutive tool errors, and routes operator guidance through
  **mid-conversation system messages** (cache-safe and injection-safe). A `StepHook` lets the login
  engine watch every step and inject state.
- **`tools.py`** — the tool surface the model gets: `goto, click, type, press, select, scroll,
  wait_for, read_page, screenshot, download_file, extract_table, request_otp, request_human,
  request_approval, finish`. `type` takes `secret:<name>` references. The `ToolExecutor` runs them,
  paces actions like a human, and enforces the policy.
- **`policy.py`** — `ActionPolicy`. **Every interactive call is classified in code**: `ALLOW`,
  `NEEDS_APPROVAL`, or `DENY`. Submits are allowed only in sign-in / filter / search forms; controls
  labelled pay / transfer / add payee / delete / change / agree / … require approval; navigation and
  links are constrained to the task's domain allowlist. When unsure it asks. This is backed by a
  **network-level block**: `ToolExecutor` routes every main-frame navigation and aborts any that
  leaves the allowlist, so a page-initiated redirect can't exfiltrate either.
- **`approvals.py`** — `ApprovalGate`. A `NEEDS_APPROVAL` action blocks the run until the owner taps
  Approve/Deny on a chat/portal card (with a masked screenshot of the page), or it times out
  (counts as deny). The model can *request* approval but can never grant it — so untrusted page text
  can't talk the agent into a payment.
- **`recorder.py`** — captures navigation statuses, console errors, failed requests and `Set-Cookie`
  presence for the diagnosis evidence.
- **`llm.py`** — `AnthropicLLM`: adaptive thinking, explicit effort, prompt caching, server-side
  refusal fallbacks, USD cost accounting per call. The `LLM` protocol is the seam the whole system
  tests against with a scripted/heuristic fake (no API key needed for tests).

**What to probe here:** prompt-injection resistance (page text is wrapped and labelled untrusted;
approvals are out-of-band), the completeness of the risky-action regex in `policy.py`, and whether any
interactive path bypasses `ToolExecutor._gate`.

---

## 7. The login engine and diagnosis (`friday/login/`)

- **`classifier.py`** — classifies a page into one state from DOM text, URL, title, inputs and HTTP
  status: `LOGGED_IN, LOGIN_FORM, NEEDS_OTP_EMAIL/SMS/TOTP, NEEDS_APPROVAL_IN_APP, SECURITY_QUESTION,
  CAPTCHA, CREDENTIALS_REJECTED, SILENT_BOUNCE, ACCESS_DENIED_OR_BLOCKED, PAGE_FAILED, UNKNOWN`. Rules
  first (deterministic, unit-tested); the model is consulted only to confirm `LOGGED_IN` and to
  resolve `UNKNOWN`/low-confidence, using a masked screenshot.
- **`engine.py`** — `LoginEngine`, a state machine driven by the agent loop's step hook. Hard rules:
  reuse a valid session (never re-login needlessly); a rejected password is retried **at most once**
  (repeated bad passwords lock real accounts); no state may repeat more than twice; an empty submit
  (password not actually typed) doesn't count as a rejection; an edge block stops with a
  "re-seed / change egress" message and **never** retries. `LOGGED_IN` is always model-confirmed
  before success is declared.
- **`diagnosis.py`** — when a silent bounce or block happens: capture evidence (final URL, nav
  statuses, console errors, cookies, masked screenshot, visible text), probe the **fingerprint** and
  the **egress IP geolocation**, then run **one-variable experiments** (headless vs headed, with/without
  proxy, bundled Chromium vs real Chrome, fresh vs persistent profile, and a human manual login in the
  same profile). `rank_causes()` is a pure, unit-tested function that produces a ranked list
  (automation fingerprint / IP reputation / new-device distrust / session conflict / bad credentials /
  site outage), marks anything not isolated by an experiment as **unconfirmed**, and suggests the
  cheapest next experiment. Probes never submit credentials (no lockout risk); the manual-login test
  runs only on request.

Every report also records the **environment** the site saw (effective driver, pinned
`navigator.webdriver`, proxy, egress IP/ASN). When the driver isn't patchright or the pin carries
`webdriver: true`, a manual remote-view sign-in is labelled INCONCLUSIVE: the human drives the same
flagged browser. The next step is then preflight.

- **Preflight (`friday/preflight.py`)** runs before any login, fetch or task run touches a site. It
  checks driver, proxy, egress through the proxy, identity pin and OTP channel, plus Google for sheet
  requests. A failure ends the run before the browser opens, posts the fixes to the chat, and is not
  counted as a bounce. **Profile doctor (`friday/profiles/doctor.py`)** compares the pin
  (`pinned_for`) with the config; it runs at startup and after identity changes.

This is the subsystem that produced the real Amex finding: `navigator.webdriver=true` + a
datacenter IP + an en-GB locale on an Indian IP → an Akamai 403 at the edge. The fix (patchright,
consistent locale, per-profile proxy) came straight from the ranked report.

**What to probe here:** classifier accuracy on real bank HTML (only a mock bank is in-repo — see
limitations), and whether `rank_causes` ever reports "confirmed" without an isolating experiment.

---

## 8. OTP, onboarding and site memory

- **OTP (`friday/otp/`)** — `broker.py` orchestrates: **TOTP** generated locally from a stored seed
  (`totp.py`, RFC 6238, tested against the RFC vectors); **email** via read-only Gmail
  (`gmail.py`, OAuth PKCE, scope `gmail.readonly`, matches sender + time-after-submit, extracts the
  code with `extract.py`, discards the mail); **SMS / push** asked of the owner through the chat or an
  Android SMS-forwarder webhook. Codes are single-use, redacted, never logged. `extract.py` scores
  candidate numbers so it ignores amounts, years, dates and card endings.
- **Onboarding (`friday/onboarding.py`)** — the "add an account from the chat" flow. `SiteFinder` uses
  the model *with web search* to find a provider's official sign-in page; the proposal is validated in
  code (HTTPS, login host inside the proposed domains, real domains, reachability) and is **never
  saved until the owner confirms it on a setup card**. The **setup card** is a chat card whose form
  posts straight to `/api/setup/<id>` → the vault; the values never pass through the model, the chat
  history or the logs (the endpoint parses the body by hand to avoid FastAPI echoing it in a
  validation error). The card carries the original job and **resumes it automatically** once submitted.
  It can also repair a broken site record.
- **Site memory (`friday/sitenotes.py`)** — short free-text notes the agent writes after a run (login
  URL, flow quirks, where statements live, typical waits, official export options) and reads at the
  start of the next run. Notes are **hints, never executable steps**, are redacted, and are marked
  *stale* when a run contradicts them.

**What to probe here:** the setup endpoint's no-echo guarantee, the proposal validation (can a
search-poisoned result get a credential typed into the wrong domain? — the injector's frame check is
the backstop), and that site notes can never become instructions.

---

## 9. Capabilities and the job queue

- **`capabilities/statement.py`** — fetch a statement for a date range: the agent downloads the file
  (prefers the site's official CSV/export, else PDF), then `transactions.py` parses it. CSV/XLSX are
  parsed structurally (header mapping, day/month order inferred from the data, amount/direction/balance
  normalisation); PDFs have their text extracted and rows pulled by the model under a strict JSON
  schema. Everything is then **verified in code** (date range, running-balance continuity, stated
  totals) before it is trusted, and **deduped** by a stable hash over
  `(source, date, amount, direction, normalised description, balance)` plus an in-file occurrence index
  — so the same statement fetched twice adds nothing, but two identical real transactions on one day
  are both kept.
- **`capabilities/task.py`** — an open-ended task the owner described in words, on one signed-in
  account, under the same policy/approval rules.
- **`jobs.py`** — `JobRunner`: **one worker per profile** (an account is never driven twice at once),
  a **per-site lock** (one login per site at a time) with a minimum gap, **exponential backoff** after
  blocks/bounces/rejections, and a global cap on concurrent browsers. A diagnosis is queued
  automatically after a block. Runs are persisted (`runs.py`) with a full step log and masked
  screenshots.

---

## 10. Web surfaces and auth (`friday/web/`)

- **`auth.py`** — single-user: one argon2-hashed password, a signed session cookie (key derived from
  the master key), same-origin checks on every state-changing request *and* on the VNC WebSocket,
  exponential backoff after failed logins, `Secure` cookies when served over HTTPS.
- **Chat (`chat.py`, `web/chat_api.py`, `web/static/chat.*`)** — the primary UI. Long-polls for
  messages and live cards (OTP, approval, handoff, run, setup). The owner's typed code goes to the
  waiting sign-in and never to the model; anything else goes to the assistant. Card submissions
  (approve/deny, code, credentials) are deliberate taps, out of band from the model.
- **Portal (`web/api.py`, `web/app.py`, `web/static/portal.*`)** — backup/inspection: accounts and
  session status, runs with step logs and masked screenshots, approvals, OTP queue, handoffs,
  diagnosis reports, downloaded statements and transactions (CSV export), secrets (names only), sites
  and site notes, and a hash-chained **audit log** viewer.
- **Remote-view handoff (`handoff/`)** — when a human is needed, friday posts a card linking to an
  authenticated noVNC view of that exact browser session (WebSocket↔VNC bridge, localhost-bound VNC).
  The owner finishes the step and clicks Done; the agent resumes in the same profile. This is also how
  a profile is first seeded by a manual login. A connected view keeps the handoff alive, and a typing
  box sends text as RFB key events when the canvas doesn't get the keyboard.
- **`audit.py`** — append-only, **hash-chained** audit log of every action and every secret use (by
  name). The chain is verifiable; the portal shows whether it is intact.

---

## 11. Deployment (`deploy/gcp/`, `Dockerfile`, `docker-compose.yml`)

- **One Docker image**: real Google Chrome (amd64) + Xvfb + x11vnc + vendored noVNC + the app, with
  `tini` as init (reaps Chrome's helper processes) and Chrome's sandbox kept on via a custom seccomp
  profile (`scripts/gen_chrome_seccomp.py` = Docker's default + the namespace/chroot syscalls Chrome
  needs), `cap_drop: ALL`, `no-new-privileges`.
- **Target**: one GCE VM in Mumbai (`asia-south1`) with a static IP, a persistent data disk (profiles
  + SQLite + encrypted files survive rebuilds), and a dedicated service account. Secrets (master key,
  portal password hash, Anthropic key) live in **Secret Manager**, read into `.env` on boot by a
  startup script.
- **Ingress**: SSH only from Google's IAP range; the portal/chat reached either through an IAP SSH
  tunnel (private) or a **Caddy HTTPS front end** on a free `sslip.io` domain with an automatic
  Let's Encrypt certificate (public mode — password-only, the owner's choice).
- **`deploy.sh`** at the repo root is one step: sign in to Google if needed, then ship the current
  commit (git archive → scp over IAP → rebuild → restart → health check). It currently runs live at
  `https://34-14-129-151.sslip.io`.
- **Egress IP** is the known weak point for bank anti-bot (see §7): the design supports a per-profile
  proxy, and a built-in home-egress proxy (`friday/egress.py`) so traffic can exit on a residential IP.

---

## 12. Testing

- **183 unit tests** (no network, no API key): crypto and masking, the secret injector's refusals,
  profile isolation and the open-lock, the page-state classifier, OTP extraction + TOTP vectors, the
  action policy, the approval gate, diagnosis ranking, transaction parsing/verification/dedupe, the
  onboarding flow and its no-secret-leak guarantees, web auth and the VNC bridge, and the exact
  Anthropic request shape (against a fake API).
- **17 real-Chrome tests** (`-m chrome`): the full job runner against an in-repo **mock bank**
  (`friday/mock_site.py`) covering every login scenario (normal, email/SMS/TOTP OTP, push approval,
  silent bounce → diagnosis, access denied, CAPTCHA handoff, a page that disables `eval`), statement
  fetch + dedupe, the approval gate blocking a payment, the navigation allowlist, and the
  patchright/webdriver + locale checks. The model is a generic heuristic stand-in, so these need no
  API key.

**What to probe here:** the biggest test gap is that there is **no real bank** in the loop — the
classifier, the policy regexes and the statement parsers are exercised against a mock and against
one real login page (Amex, manually), not a broad corpus.

---

## 13. Module map

```
friday/
  context.py            AppContext: builds and wires every component
  config.py             all settings (env, FRIDAY_ prefix)
  db.py                 SQLite + migrations (16 tables)
  audit.py              hash-chained audit log
  runs.py               run + step records (redacted)
  files.py              encrypted-at-rest file store
  sitenotes.py          site memory (hints, staleness)
  jobs.py               job queue: per-profile worker, per-site lock, backoff
  onboarding.py         add-account-from-chat: site finder, setup card, resume
  assistant.py          chat entry: hands requests to the planner; account/run/setup handlers
  planner.py            plan → execute → verify → report; replans; plan cards
  memory.py             long-term memory (facts, decisions, outcomes)
  scheduler.py          reminders + recurring jobs (loop over `schedules`)
  sheets.py             Google Sheets client, column matching, append/verify
  workspace.py          Gmail + Drive clients
  google_oauth.py       owner's Google grants (workspace / gmail / sheets), PKCE, vault tokens
  prefs.py              small owner preferences (saved expense sheet)
  chat.py               chat store + notifier (owner's delivery channel)
  egress.py             home egress proxy (residential IP)
  mock_site.py          local mock bank for tests/demos
  cli.py                the `friday` CLI
  vault/      crypto, store (incl. ephemeral OTP secrets), injector, masking
  drivers/    base, playwright_driver, cdp_driver  (pluggable real-Chrome launch)
  profiles/   models, manager, display (Xvfb/VNC/native), volume, (fingerprint)
  agent/      loop, tools, policy, approvals, snapshot, recorder, isolated, llm
  login/      classifier, engine (state machine), diagnosis (experiments + ranking)
  otp/        broker, gmail (read-only), totp, extract
  capabilities/  statement, activity, task, transactions (parse/verify/dedupe)
  tools/      base (Tool, ToolRegistry: the gate), accounts (browser runs), google_tools,
              web, files (table_compute), memory_tools, schedule_tools
  handoff/    manager, bridge (WebSocket↔VNC)
  web/        app, api (portal), chat_api, auth, static/(portal|chat).(html|js)
deploy/gcp/   deploy.sh, startup.sh, compose.public.yml, Caddyfile, seccomp
tests/        246 unit + 21 real-Chrome (mock bank, fixture pages), fakes, fake_google, planner_fakes
```

---

## 14. Threat model and trust boundaries

- **Untrusted:** every byte from a web page, email or downloaded file. It is wrapped and labelled as
  data for the model, never followed as instructions; it cannot trigger an approval, a navigation off
  the allowlist, or a secret into the wrong field.
- **Trusted:** the owner, via the chat/portal (authenticated), schedules the owner created, and the
  system's own prompts. Only these can start a plan.
- **New with the assistant layer:** content from Gmail, Sheets, Drive and the web is untrusted like
  page text: it reaches the planner only as quoted results or failure evidence. It cannot plant a
  memory or a schedule (those tools need the owner's own words, checked in code), cannot redirect a
  write to a sheet the owner didn't name (that needs a tap), and cannot send or share anything (always
  a tap). `web_fetch` refuses non-public addresses, re-checked on every redirect.
- **The model is semi-trusted:** it decides *what* to do but is boxed by the policy, the approval
  gate, the domain allowlist and the injector. A compromised or jailbroken model still cannot see a
  secret, pay money without a tap, or leave the allowlisted domains.
- **At rest:** secrets and files encrypted; profiles on an encrypted volume; master key in Secret
  Manager / keyring. **In transit:** HTTPS; VNC bound to localhost and bridged only through the
  authed app.
- **Residual risk the owner accepts:** in public mode the portal password is the only thing between
  the internet and the owner's logged-in sessions (with login backoff); the egress is a datacenter IP
  unless a proxy is configured; and the app trusts the single owner completely.

---

## 15. Known limitations and good questions for a reviewer

1. **No real bank in the test loop.** Everything is validated against a mock bank and one real login
   page. The classifier, policy regexes and PDF/CSV parsers are the most exposed to real-world drift.
2. **Anti-bot is an environmental fight, not a code one.** Edge blocks (Akamai etc.) depend on IP
   reputation and fingerprint; friday addresses them honestly (consistent real Chrome, patchright for
   the webdriver flag, per-profile residential egress) and refuses to escalate into evasion. A
   determined bank can still block a datacenter deployment.
3. **Single process, single VM.** Simple and auditable, but no horizontal scale and the VM is a single
   point of failure. Fine for one person; not a multi-tenant design (deliberately).
4. **The model is in the loop for parsing PDFs and resolving ambiguous pages.** That is a cost and a
   correctness surface; the code-side verification (balance continuity, stated totals, dedupe) is the
   backstop — evaluate whether it is strong enough.
5. **Public mode is password-only** by the owner's choice; a stronger deployment would put Identity-
   Aware Proxy or a private network in front.
6. **Prompt-injection surface** is broad (bank pages are hostile by nature). The defences are
   structural (untrusted-data framing, out-of-band approvals, code-enforced policy, secret references);
   a reviewer should try to defeat exactly these.
7. **TOCTOU and concurrency:** profiles are single-open-locked and jobs are per-profile serialised, but
   the whole thing is one asyncio loop — worth checking for races around card state, run cancellation,
   and the diagnosis re-open.

Suggested evaluation entry points, in order: `friday/vault/` (the core secret invariant),
`friday/agent/policy.py` + `approvals.py` (the read-only guarantee), `friday/agent/loop.py` +
`snapshot.py` (prompt-injection posture), `friday/login/` (the autonomous sign-in and its honesty
about failure), and `tests/` (what is actually proven vs asserted in prose).

---

## 16. The assistant layer: planner, tools, memory, schedules

**Tools (`friday/tools/`).** Every capability is a `Tool` with a JSON schema and a declared *effect*:
`read`, `write` (private to the owner), `send`, `share`, `spend`, `publish`. `ToolRegistry.call` is the
single choke point: it validates arguments, decides in code whether the owner must approve (always
for send/share/spend/publish; for a private write only when it targets something the owner didn't
name, something other people can see — checked via Drive permissions, failing closed — or when
`FRIDAY_APPROVE_PRIVATE_WRITES` is on), checks `owner_quote` for tools that store state for later
(memory, schedules, the saved expense sheet), audits the call, redacts the result, and marks outside
content as untrusted. Approval uses the same `ApprovalGate` card as browser runs; the model cannot
approve. Tools: `browser_run` (waits for the run, streaming plain-language progress), accounts/setup
and run state, Sheets (list, read, append rows, create tab, add stored transactions, remove only rows
friday appended — ledger `sheet_rows` — and only if unchanged), Gmail (search, read, draft; send is
gated), Drive (search, download, upload, share is gated), `web_search`, `web_fetch`, `files_read`,
`table_compute` (totals computed in code from real cells), memory, schedules.

**Planner (`friday/planner.py`).** One structured-output call returns the plan: steps with a tool,
arguments (which may reference earlier results with `{"$ref": "s1.path"}`), an optional `fill`
instruction (a small model call reads values out of earlier results), a progress line and success
checks. Steps run through the registry; after each, its checks are evaluated in code against the real
result, and tools add their own read-backs. A failed call or check replans with the failure as
untrusted evidence (at most 2), then the owner gets a blocker; a denied approval ends the plan. The
final message is written from the results; its "Verified" line comes only from passing checks and
read-backs. Plans are stored in `plans` and shown as a live card (steps + progress); "status" while a
plan runs is answered from its state without a model call.

**Memory (`friday/memory.py`).** Facts and decisions from the owner's own words, outcomes written by
code after each plan. All current facts plus relevant outcomes go into the planner's context; a newer
fact on the same subject replaces the older; "forget" is a soft delete. Site know-how stays in site
notes.

**Schedules (`friday/scheduler.py`).** A loop in the app's event loop over `schedules`: reminders post
into the chat; jobs run the owner's stored request through the planner with the same gates. Missed
slots fire once.

**Google.** One `workspace` grant (Sheets, `gmail.modify`, `drive.readonly`, `drive.file`) from a
consent link in the chat; tokens in the vault under the reserved `system/` namespace; each feature uses
the first connected grant that covers it, so the older gmail-only / sheets-only grants keep working.

