# Runbook: getting a hardened card site (e.g. Amex) to work end to end

These are owner-side steps on the live deployment. They change no code. Run them from your laptop, in the repo:

```bash
F() { deploy/gcp/deploy.sh cli "$@"; }    # runs `friday <args>` inside the container on the VM
```

The profile id is `<site>.<account>`. Find it with `F profile list`. Below it is `amex.me`.

**Every sign-in that bounces is a real login attempt against a real account.** friday refuses to start one while its prerequisites are missing (the preflight gate). Don't work around it by looping attempts.

## 0. Is the new build deployed?

```bash
curl -s https://<your-host>/healthz     # {"ok": true, "version": "<short sha>", "built_at": ...}
git rev-parse --short origin/main
```

The portal header and `/api/state` (`version`) show the same stamp. If it's old, run `deploy/gcp/deploy.sh ship`. The script waits until `/healthz` reports the new revision.

## 1. Preflight

```bash
F preflight amex.me            # per item: ok / FAIL, with the command that fixes each failure
F preflight amex.me --fresh    # look up the egress again (otherwise cached 15 min)
```

| Item | Passes when | Fix |
|---|---|---|
| driver | the profile uses `patchright` (plain Playwright shows `navigator.webdriver = true`) | `F profile set-driver amex.me patchright` |
| proxy | a proxy is configured | step 2 |
| egress | the IP seen **through the proxy** is not a datacenter/hosting ASN and is in the profile's country (en-IN / Asia/Kolkata → IN) | step 2 |
| identity | the pinned fingerprint was taken under the current driver, locale, timezone and proxy, and agrees with them | `F profile doctor amex.me --repin` |
| otp | always passes; it reports the live channel (email, sms or the chat code card) | optional, step 3 |
| google | (for "populate the sheet" requests) Google is connected | say "connect Google" in the chat |

A run that fails preflight never opens the site. It posts one chat message listing the failures and fixes, and doesn't count as a bounce or start the backoff. `FRIDAY_PREFLIGHT=warn` only logs; `off` disables the gate. For one deliberate attempt despite failures:

```bash
F preflight amex.me --override   # the next sign-in in the next 30 minutes runs anyway; a bounce still counts
```

## 2. One stable home egress (no rotation)

The live Amex test bounced from the VM's Google Cloud IP, even when signing in by hand. One command routes the profile through your home connection:

```bash
TS_AUTHKEY=tskey-... deploy/egress.sh amex.me <home-tailscale-ip>     # TS_AUTHKEY optional: without it you get a login URL
```

It installs Tailscale on the VM, prints the command to run on the home machine (in a friday checkout, on the same tailnet):

```bash
FRIDAY_EGRESS_PASSWORD='<printed>' uv run friday egress-proxy --listen <home-tailscale-ip>:3128
```

Then it waits for the proxy and sets the profile's proxy and the patchright driver; the password goes into the vault via stdin. Last, it runs preflight and **refuses to finish if the egress is a datacenter ASN or outside India**. Setting the proxy re-pins the identity automatically. `/api/state` → `egress` shows the IP, ASN and proxy; every diagnosis report records them too.

## 3. One-time codes

Amex India sends login codes by SMS. The default is the chat's code card: friday asks, you type the code into the chat. That works now and needs no setup. For hands-free codes, either:

- **SMS forwarder:** set `FRIDAY_SMS_WEBHOOK_TOKEN` and point an Android SMS-forwarder app at `https://<host>/hooks/sms`; it POSTs JSON `{"text": ...}` with header `X-Friday-Token: <token>`. The channel shows as live after the first forwarded message.
- **Email, if the site emails codes:** `F site set-otp-senders amex AmericanExpress@welcome.americanexpress.com`, then say "connect my Gmail for codes" in the chat.

`/api/state` → `otp_channels` shows which one is live per site.

## 4. Re-seed once, by hand (after preflight is green)

In the chat, say "let me sign in to Amex myself". A remote-view card opens the same patched profile; sign in once (tick "remember this device"), then tap **I'm done**.

- The handoff stays open 30 minutes, and is extended while the remote view is connected (up to 4 hours).
- If the canvas doesn't take your keyboard, use the **Type into the browser** box above it: click the field in the remote browser, type into the box, press Type. Tick "then Enter" to submit.
- Under plain Playwright or a `webdriver: true` pin, a manual bounce is **inconclusive**: you drive the same flagged browser. The diagnosis report says so and points back to preflight.

If an automated sign-in later bounces, friday runs the diagnoser and stops. The backoff starts at 15 minutes and doubles. Read the diagnosis report before trying again.

## 5. Acceptance (live)

1. `F preflight amex.me --google` is all green: patchright, residential Indian egress, fresh pin, OTP channel, Google connected.
2. In the chat: "Fetch and populate my Oct Amex expenses into the sheet".
   - The first run asks which sheet and tab.
   - It signs in, using the code card if a code is needed, and reads the activity page.
   - It appends exactly the fetched October charges to the tab.
   - It reports fetched == appended == read-back and the October total.
3. The same request again adds zero rows.
4. `/api/state` shows `version`, `egress.amex.me.asn` (a residential ISP) and `otp_channels.amex`.
