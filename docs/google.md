# Connecting Google (Gmail codes, Sheets)

friday uses two separate Google grants, each with only what it needs:

| Grant | Scopes | Used for |
|---|---|---|
| Gmail | `gmail.readonly` | Reading emailed one-time codes (matched by the site's OTP senders, after the sign-in submitted), and **expense evidence** from bank/card/merchant emails ("get my expenses from email"). Read-only: friday never sends, drafts, labels or deletes mail for either. |
| Sheets | `spreadsheets`, `drive.metadata.readonly` | Listing your spreadsheets, reading a tab, appending rows |

Refresh tokens are stored encrypted in the vault under `system/google/…` (Gmail keeps its older name `system/gmail/refresh-token`). `system` is a reserved site id, so no account profile can reach them: the injector can't type one into a page, and they never enter the model's context.

## One-time: create an OAuth client

1. In Google Cloud Console (any project, e.g. `rv-experiments`), go to **APIs & Services → Library** and enable the **Gmail API**, the **Google Sheets API** and the **Google Drive API**.
2. Go to **APIs & Services → OAuth consent screen**:
   - User type: External.
   - Publishing status: Testing.
   - Under Test users, add your own Google address.
   In Testing mode the grant expires after 7 days; publish the app (for your own use, no verification needed for personal use) to avoid reconnecting weekly.
3. Go to **Credentials → Create credentials → OAuth client ID → Web application**, and add the authorized redirect URIs:
   - `http://127.0.0.1:8765/oauth/google/callback` (portal through the IAP tunnel, or local)
   - `https://<your-domain>/oauth/google/callback` (if you use `deploy/gcp/deploy.sh public`, e.g. `https://34-14-129-151.sslip.io/oauth/google/callback`)

   The redirect URI is `FRIDAY_PUBLIC_BASE_URL` + `/oauth/google/callback`, so it must match exactly what the server uses.
4. Give the ID and secret to the server:
   - GCP VM: `deploy/gcp/deploy.sh google-client`. It stores both in Secret Manager (labelled `app=rv-fri`) and restarts friday.
   - Local: set `FRIDAY_GOOGLE_CLIENT_ID` and `FRIDAY_GOOGLE_CLIENT_SECRET` in `.env`.

## Connect

Ask in the chat ("connect my Gmail for codes", "connect Google Sheets"); friday posts a link. Or open:

- `<portal>/oauth/google/start?purpose=gmail`
- `<portal>/oauth/google/start?purpose=sheets`

Tick every box on Google's consent screen. A partial grant is refused, and nothing is stored.

How the callback is protected: Google redirects back as a cross-site navigation, so the portal's `SameSite=Strict` session cookie isn't sent. The callback is instead authorised by the `state` value, which is single-use, expires after 10 minutes, and can only come from a logged-in `/start`. PKCE ties the code to this server.

## The expense sheet

The first time you ask friday to populate a sheet, it asks which spreadsheet and tab (it can list them), checks that the tab has Date, Description and Amount columns (Debit/Credit, Currency, Type and Card columns are filled when present), and saves that as your expense sheet.

Appends:
- use `INSERT_ROWS`, so existing rows are never overwritten;
- leave note, comment, category and any unrecognised columns blank in new rows;
- skip transactions already in the tab;
- write website text that starts with `=`, `+`, `-` or `@` as plain text, never a formula.

By default only charges are added. Refunds and card payments come in only if you ask (`include_credits`), as negative amounts. After every append the tab is read back, and the chat reports the row count and the total for the range.
