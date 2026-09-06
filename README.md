# Weekly arXiv digest

Two relevant papers, emailed (or Telegrammed) to you every Monday and Friday.
Runs on GitHub Actions, so there's no server and no cost.

## Repo layout

```
your-repo/
├── digest.py
├── sent.json                        <- starts as [], the workflow commits it back
└── .github/
    └── workflows/
        └── weekly-papers.yml
```

No dependencies — standard library only.

## Setup

**1. Create the repo** and drop in the two files above. Public or private both
work; private repos use your free Actions minutes, and this job takes about a
minute a week.

**2. Add your delivery credentials** under
Settings → Secrets and variables → Actions → New repository secret.

For email (Gmail example):

| Secret | Value |
| --- | --- |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | a Google **App Password**, not your login password |
| `EMAIL_TO` | where to send (optional, defaults to `SMTP_USER`) |

App Passwords require 2-Step Verification to be turned on, then are generated
at myaccount.google.com → Security → App passwords.

For Telegram, via your own bot (free, official API):

| Secret | Value |
| --- | --- |
| `TELEGRAM_TOKEN` | the token @BotFather gives you |
| `TELEGRAM_CHAT_ID` | your own chat id, a number |

One-time setup: message **@BotFather** on Telegram, send `/newbot`, pick a name,
and it replies with a token. Then send your new bot any message — a bot can't
open a conversation with you — and open
`https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. Your chat id is
the `"chat":{"id":...}` number in the response.

This is the lightest of the three to set up: no account to create, no 2FA, and
the token only controls a bot that can message you, so it's worth far less than
a Gmail App Password if it ever leaks.

For WhatsApp, via CallMeBot (free, no registration):

| Secret | Value |
| --- | --- |
| `WHATSAPP_PHONE` | your number with country code, e.g. `+15551234567` |
| `WHATSAPP_APIKEY` | the key CallMeBot messages back to you |

One-time setup: save **+34 684 72 39 62** to your contacts, WhatsApp it
`I allow callmebot to send me messages`, and it replies with your API key.
If nothing arrives within two minutes, wait 24 hours and retry.

Set any combination of the three. If none are set, the script just prints the
digest to the Actions log — useful for a first test. Each channel is
independent: if one is half-configured or its service is down, the script says
which secret is missing and still sends on the others.

### Why CallMeBot and not the official WhatsApp API

Meta's WhatsApp Business Cloud API is the "proper" route, but it's a poor fit
for a personal weekly digest:

- Business-initiated messages outside a 24-hour window must use a
  **pre-approved template**, which you submit to Meta and wait on.
- Since July 2025 Meta bills **per delivered template message** rather than per
  conversation window. Customer-initiated service messages are free, but a
  scheduled digest is never customer-initiated, so every send is billable.
- Template bodies cap at 1024 characters, which is tight for paper abstracts.
- It requires a Meta Business account and a dedicated phone number.

CallMeBot sidesteps all of that with a single HTTP GET. The tradeoffs, worth
knowing: it's an unofficial one-person hobby project with no relationship to
Meta, it's free **for personal use only**, and your message text passes through
their server. For public arXiv titles and links that's harmless — don't route
anything private through it.

Gmail has none of these caveats and is the most reliable of the three. Telegram
is the middle ground: an official, free API with no billing and no templates,
and a 4096-character limit that leaves room for a short abstract. Running
Telegram as the phone ping and email as the readable copy works well.

**3. Test it.** Actions tab → "Weekly paper digest" → Run workflow. Check the
log output before trusting the cron.

## Tuning

Everything you'd want to change is in the `CONFIG` block at the top of
`digest.py`:

- `CATEGORIES` — arXiv categories to pull from
- `KEYWORDS` — keyword → weight; title matches count double
- `MIN_SCORE` — quality floor, so a slow week sends nothing rather than junk
- `PAPERS_PER_RUN` — currently 2

Raise `MIN_SCORE` if you're getting noise, lower it if you're getting silence.
Watch the first few runs and adjust from there — keyword weights always need a
round or two of tuning against real output.

## Two gotchas worth knowing

**Scheduled workflows get disabled after 60 days of repo inactivity.** GitHub
does this to every repo. This one dodges it because each run commits `sent.json`
back, which counts as activity. If you ever change it to not write state, you'll
need to push something manually every couple of months.

**Cron times are UTC and are not precise.** Scheduled jobs queue behind everyone
else's and often fire 5–30 minutes late, occasionally more. Fine for a weekly
digest; don't use it for anything time-critical.

## Note on the arXiv API

The script sleeps 3 seconds between pages, which is what arXiv asks for. Don't
lower it — sustained hammering gets IPs blocked. The API needs no key and has
no auth.
