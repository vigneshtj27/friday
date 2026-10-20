# Runbook: getting a hardened card site (e.g. Amex) to work end to end

These are owner-side steps on the live deployment. They change no code. Run the CLI inside the container:

```bash
F() { gcloud compute ssh rv-fri --zone=asia-south1-a --tunnel-through-iap -- \
  "cd /opt/friday/app && sudo docker compose exec -T friday friday $*"; }
```

The profile id is `<site>.<account>`. Find it with `F profile list`. Below it is `amex.me`.

## 1. Check, then fix, the profile's browser identity

```bash
F profile show amex.me          # look at config.driver, config.proxy, config.locale, config.timezone
```

If `driver` is `null` (plain Playwright: `navigator.webdriver` is true), switch to the opt-in patchright driver. Then set one consistent identity:

```bash
F profile set-driver amex.me patchright
F profile set-identity amex.me --locale en-IN --timezone Asia/Kolkata
```

Both commands clear the pinned fingerprint, so the next open re-pins it.

## 2. One stable home egress (no rotation)

A datacenter IP (Google Cloud, Mumbai) was the second-ranked cause in the Oct 18 diagnosis.

1. On an always-on machine at home, run the built-in proxy, bound to its Tailscale address. Set `FRIDAY_EGRESS_PASSWORD` (16+ characters) on that machine first.

   ```bash
   friday egress-proxy --listen <home-tailscale-ip>:3128
   ```

2. Give the VM a route to that address. Install Tailscale on the VM and join the same tailnet.
3. Point the profile at it. The proxy password is prompted and stored in the vault:

   ```bash
   F profile set-proxy amex.me --server http://<home-tailscale-ip>:3128 --username friday
   ```

One proxy per profile, never rotated. The diagnosis report flags a timezone/IP mismatch if the home IP isn't in India.

## 3. Gmail for codes

1. Do the one-time OAuth client setup in [google.md](google.md), then `deploy/gcp/deploy.sh google-client`.
2. Add the site's OTP sender:

   ```bash
   F site set-otp-senders amex AmericanExpress@welcome.americanexpress.com
   ```

3. In the chat, say "connect my Gmail for codes" and open the link.

The chat's code card stays as the fallback if no email arrives within about 90 s.

## 4. Re-seed once, by hand

In the chat, say "let me sign in to Amex myself". A remote-view card opens the same patched profile; sign in once (tick "remember this device"), then tap **I'm done**.

Don't loop sign-in attempts. If an automated sign-in later bounces, friday runs the diagnoser and stops (backoff starts at 15 minutes and doubles). Read the diagnosis report before trying again.

## 5. Then

Say: "Fetch and populate my Oct Amex expenses into the sheet". The first time, friday asks which spreadsheet and tab, and posts a Sheets connect link if needed.
