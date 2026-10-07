# EA Tax Resolutions — SMS Follow-up (proof of concept)

Follows each Mixmax sequence email with a scheduled RingCentral text message. When a stage's email goes out to a lead, the app schedules that stage's text (for example, 60 minutes later), re-checks for replies, opt-outs and sequence status at the due time, then sends it (or only simulates it in test mode).

## Features

- **Sequences:** every Mixmax sequence with its stages in order. Each stage can have a text with its own delay, sent automatically or held for review first. AI (OpenAI) can draft all stage texts from the stage emails.
- **Recipients:** everyone in the Mixmax sequences, with their Mixmax status, last stage, next email and next text. Filter by text status or stage, search, and take bulk actions. A side panel holds each person's phone-number choice, stop conditions and text history.
- **Texts:** one row per person per sequence, which expands to show each stage's text. Drafts can be reviewed, edited, personalized with AI, approved or cancelled.
- **Safety:**
  - Test mode simulates sends.
  - Live sends require an allowlisted number and approval.
  - Any inbound text (including STOP) suppresses that number.
  - Mixmax email replies stop texts automatically.
  - Texts more than 24 hours overdue are held or expire.
  - When a contact has several numbers, staff choose which one gets texts.

## Stack

Python 3.10+ standard library only (no pip dependencies), SQLite, and a single-page dashboard (`templates/dashboard.html`, vanilla JS).

| File | Purpose |
|---|---|
| `app.py` | Local HTTP server, JSON API and background poll/send loop |
| `engine.py` | Scheduling, stop conditions, Mixmax recipient sync and data views |
| `providers.py` | Mixmax and RingCentral API clients (outbound only) |
| `drafts.py` | SMS drafting and personalization (OpenAI) |
| `discover.py` | Read-only check of the configured accounts |

## Run locally

```
cp .env.example .env      # fill in credentials; defaults run in demo mode
python3 app.py            # opens http://127.0.0.1:8050
```

`DATA_MODE=demo` runs on fake data with no API calls. `DATA_MODE=live` with `TEST_MODE=true` reads the real Mixmax and RingCentral accounts but only simulates texts.

## Status

This is a proof of concept. The dashboard has no login and only accepts connections from the local machine (`127.0.0.1`), so it is not ready to be exposed on the internet as-is. Hosting would need authentication, configurable allowed hosts, persistent storage for the SQLite database, and a single running instance.

`.env` and the `*.sqlite3` databases (credentials, lead names, phone numbers, message history) are deliberately excluded from this repository.
