# CourseTrack

Pulls your Canvas coursework every hour, mirrors each deadline into a dedicated
Google Calendar, and emails you **3 days** and **12 hours** before anything is
due — plus a 7am summary of the week. Runs on GitHub Actions, so it works
whether or not your laptop is open.

No AI at runtime. The scheduled job is deterministic Python with two
dependencies.

```
GitHub Actions cron (hourly, :05)
        │
        ├─→ Canvas REST API ──── courses, assignments, your submission state
        ├─→ Google Calendar ──── reconcile a "Coursework" calendar
        ├─→ Gmail via SMTP ───── threshold digests + the 7am brief
        │
        └─→ commit state back to the repo
               data/deadlines.json   public, cleartext
               data/private.enc      encrypted

local: coursetrack dash → 127.0.0.1:8787
```

## What is and isn't public

This repo is public, so exactly one file is readable by anyone:

`data/deadlines.json` — course name, assignment title, due date, Canvas link.

Everything else is Fernet-encrypted into `data/private.enc`: your email address,
submission states, calendar event IDs, reminder history, and any deadline you
mark **private** (those show as `Private deadline` in the public file, with the
real title kept locally and on your own calendar).

Credentials are never in the repo at all — they are GitHub Actions secrets,
encrypted at rest and masked in logs. `scripts/check_public_file.py` runs on
every tick and fails the job before the commit step if anything private, or
anything credential-shaped, reaches the public file.

## Setup

### 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dashboard,dev]"
.venv/bin/pytest -q          # should be all green
```

Set `canvas.base_url` in `config.toml` to your institution's Canvas host.

### 2. Generate the state key

```bash
.venv/bin/python -m coursetrack init-key
```

Stores it in your login Keychain and prints it once. Save that value — you need
it as the `COURSETRACK_KEY` secret, and losing it means losing the reminder
ledger (calendar links rebuild themselves; the ledger does not).

### 3. Canvas token

Canvas → **Account → Settings → + New Access Token**. Copy it into `.env` as
`CANVAS_TOKEN`.

### 4. Google Calendar

In the [Google Cloud console](https://console.cloud.google.com):

1. Create a project and **enable the Google Calendar API**.
2. **OAuth consent screen** → External → fill in the required fields →
   **PUBLISH APP** so the status reads *In Production*.

   > This step is not optional. While the consent screen is in *Testing*,
   > Google expires refresh tokens after **7 days** and the job dies every
   > week. `calendar` is a "sensitive" scope, so an unverified personal app
   > shows one "Google hasn't verified this app" screen — choose **Advanced →
   > Go to … (unsafe)**. That warning is expected and only appears once.

3. **Credentials → Create credentials → OAuth client ID → Desktop app.** Put
   the client ID and secret into `.env`.
4. Mint the refresh token:

   ```bash
   .venv/bin/python -m coursetrack auth
   ```

   Add the printed value to `.env` as `GOOGLE_REFRESH_TOKEN`.

### 5. Email

Requires 2-Step Verification on the sending Google account. Create a 16-character
app password at <https://myaccount.google.com/apppasswords> and set
`GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`. Then pick a recipient and test:

```bash
.venv/bin/python -m coursetrack set-email you@example.com
.venv/bin/python -m coursetrack test-email
```

### 6. First sync

```bash
.venv/bin/python -m coursetrack sync --dry-run   # preview, writes nothing
.venv/bin/python -m coursetrack sync             # real
.venv/bin/python -m coursetrack list
```

### 7. Ship it to GitHub

Create the repo and push, then:

**Settings → Secrets and variables → Actions**, add:

| Secret | Value |
| --- | --- |
| `COURSETRACK_KEY` | from step 2 |
| `CANVAS_TOKEN` | from step 3 |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | from step 4 |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` | from step 5 |
| `STATE_PAT` | see below |

`STATE_PAT` is a **fine-grained** personal access token scoped to *only this
repository* with **Contents: Read and write**. The workflow pushes state with
it rather than `GITHUB_TOKEN` for two reasons: least privilege, and because a
PAT push counts as repository activity — GitHub auto-disables scheduled
workflows on public repos after 60 days of inactivity, and `GITHUB_TOKEN`
pushes do not reset that clock.

Also turn on **Settings → Code security**: *Secret scanning* and *Push
protection*.

Then trigger **Actions → tick → Run workflow** once to confirm it works
end to end.

## Daily use

```bash
.venv/bin/python -m coursetrack dash     # the dashboard, at 127.0.0.1:8787
```

Shows everything grouped by urgency with a live countdown, which reminders have
already fired, and per-course toggles. Adding or dismissing a deadline commits
and pushes automatically, so the next hourly run picks it up.

The dashboard binds to loopback only — it serves decrypted state, including
real titles for private entries.

### Adding things Canvas doesn't know about

Use the dashboard form, or:

```bash
coursetrack add "Fellowship application" "2026-11-01T17:00" --course "Admin"
coursetrack add "Visa appointment" "2026-10-24T09:00" --private
```

`--private` keeps the title out of the public file while still creating the
calendar event and sending the reminders.

You can also paste a screenshot of a syllabus or assignment page into a Claude
Code session in this repo and ask for the deadlines to be added — that writes
the same rows through the same CLI. It happens at authoring time; nothing the
scheduled job does involves a model.

## Commands

| Command | |
| --- | --- |
| `tick` | the full scheduled run: sync, calendar, reminders |
| `sync` | same, but sends no email |
| `list` | upcoming deadlines in the terminal |
| `add` / `remove` | manage manual deadlines |
| `dash` | the local dashboard |
| `status` | state health and which secrets are present |
| `test-email` | one test message |
| `auth` / `init-key` / `set-email` | one-time setup |

Useful flags on `tick` and `sync`:

```bash
# Compute everything, change nothing — prints the emails it would send.
coursetrack tick --dry-run

# Time-travel. Test threshold behaviour without waiting for a real deadline.
coursetrack tick --dry-run --now 2026-09-25T12:00

coursetrack tick --skip-calendar --skip-email
```

## Configuration

Non-secret settings live in `config.toml`: timezone, Canvas host, reminder
thresholds, the brief hour, calendar name, block length. Changing
`thresholds_hours` affects only deadlines that haven't already crossed the old
threshold — the ledger is keyed by hours, so adding a new one back-fires it for
anything still in range.

## Troubleshooting

**`invalid_grant` from Google.** The consent screen fell back to *Testing*, or
access was revoked. Re-publish it, re-run `coursetrack auth`, update the secret.

**Canvas 401.** The access token was revoked or expired. Make a new one in
Canvas settings.

**Gmail rejects the login.** `GMAIL_APP_PASSWORD` must be a 16-character app
password, not your account password, and 2-Step Verification must be on.

**No email but the run was green.** Check `coursetrack status` for the
recipient, and the dashboard for `3d sent` / `12h sent` pills — a reminder
already recorded in the ledger will not resend. The ledger is deliberate: it is
what makes retries and manual dispatches safe.

**Scheduled runs stopped.** Check Actions for a "workflow disabled" banner
(the 60-day rule) and re-enable it. Scheduled runs are also routinely delayed
5–20 minutes; the reminder logic tolerates that by design.

## Not built yet

Gradescope — it has no public API and CMU logs in through SSO, which can't be
scripted without storing university credentials and defeating MFA. The
`sources/` package is the seam it would plug into: one module implementing
`fetch(now) -> FetchResult` and nothing else changes.
