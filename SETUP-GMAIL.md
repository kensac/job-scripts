# Gmail connect — your setup steps

Everything that can be done headlessly is done. Google exposes no API for
external OAuth consent screens or standard OAuth clients, so these three are
console-only. Should take about ten minutes.

The Gmail API is already enabled on the `localscripts` GCP project — homelab
did that. Start at step 1.

---

## 1. OAuth consent screen

<https://console.cloud.google.com/auth/overview>

Make sure the project selector at the top says **localscripts**.

1. **Audience** → **External** → Create
2. App name: `Job Tracker`. User support email: your gmail. Developer contact:
   your gmail. Save.
3. Go to **Audience** in the left nav. Under **Test users** → **Add users** →
   add `kanishksachdev@gmail.com` → Save.
4. Leave publishing status as **Testing**. Do not click "Publish app" — that
   starts a verification review we do not want.
5. Go to **Data access** in the left nav → **Add or remove scopes** → paste
   this into the filter box and tick it:

   ```
   https://www.googleapis.com/auth/gmail.readonly
   ```

   → **Update** → **Save**.

Google will warn that `gmail.readonly` is a restricted scope. That is expected
and fine in Testing mode.

---

## 2. OAuth client

<https://console.cloud.google.com/auth/clients>

1. **Create client**
2. Application type: **Web application**
3. Name: `Job Tracker web`
4. Under **Authorised redirect URIs**, add both of these exactly — trailing
   slashes and http vs https matter, Google requires an exact match:

   ```
   https://www.kanishksachdev.com/job-tracker/settings/gmail/callback
   http://localhost:3000/job-tracker/settings/gmail/callback
   ```

5. **Create**. Copy the **Client ID** and **Client secret** off the dialog.

Send both to homelab-config — the client ID goes into compose as a literal
(it is public by OAuth design), the secret into Infisical. Do not paste the
secret into chat with me; I do not need to see it.

> Vercel preview deployments will not work with Gmail connect. Google matches
> redirect URIs exactly and preview URLs are generated per-deploy. Production
> and localhost only.

---

## 3. Takeout — your mail history

<https://takeout.google.com>

This is the 15 months of history. Without it the feature only knows about mail
that arrives from today onward.

1. **Deselect all**, then tick **Mail** only.
2. Click **All Mail data included** → untick **Include all messages in Mail** →
   select only the labels that hold job mail. If you have not labelled it,
   leave everything selected; the classifier is designed to sort relevance out
   and a too-narrow export loses outcomes permanently.
3. Next step → Destination **Send download link via email**, Frequency
   **Export once**, Type **.zip**, Size **50 GB** (fewer files to handle).
4. **Create export.** Google takes anywhere from minutes to a couple of days.
5. When the link arrives, download it and tell me the path. Do not move it into
   the repo — put it in `~/personal/localscripts/` and I will read it from
   there.

The export is an `.mbox` file. That is what the backfill parses.

---

## What happens after

Once the client ID and secret are on the fleet, a **Connect Gmail** button
appears in tracker settings, gated to `infra-admins`. You click it once,
approve Google's consent screen, and the ingest starts.

You will need to re-approve roughly every 7 days. That is a Google constraint
on apps in Testing mode with restricted scopes, not something in our control —
you accepted this tradeoff to avoid an account-wide app password and a paid
verification review. If it becomes annoying, the two escapes are paid Google
verification, or switching to IMAP with an app password. The credential storage
is per-user from day one specifically so either swap is a provider change
rather than a rewrite.

Access is read-only. The code only ever issues Gmail read calls, the refresh
token is encrypted at rest with the same machinery as your BYO API keys, and
you can revoke it any time at
<https://myaccount.google.com/permissions>. 
