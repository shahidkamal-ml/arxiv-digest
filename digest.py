#!/usr/bin/env python3
"""
Weekly arXiv digest.

Pulls recent papers from chosen arXiv categories, scores them against your
keywords, picks the top N you haven't seen, and sends them to you.

Standard library only - no pip install needed.
"""

import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

# ----------------------------------------------------------------------------
# CONFIG - edit this block, everything else can stay as is
# ----------------------------------------------------------------------------

CATEGORIES = ["cs.CV", "cs.AI", "cs.LG"]

# Keyword -> weight. Matched against lowercased title + abstract.
# Title matches count double automatically.
KEYWORDS = {
    "long-horizon": 4,
    "motion": 3,
    "temporal reasoning": 3,
    "video understanding": 3,
    "spatiotemporal": 3,
    "video benchmark": 3,
    "physics": 2,
    "video": 2,
    "benchmark": 2,
    "vision-language": 2,
    "action recognition": 2,
    "dynamics": 1,
    "evaluation": 1,
}

# Drop anything scoring below this, even if it's the best of a weak week.
MIN_SCORE = 4

PAPERS_PER_RUN = 2
LOOKBACK_DAYS = 8          # slight overlap with a 7-day cadence, dedupe handles it
MAX_FETCH = 400            # how many recent papers to consider
STATE_FILE = Path("sent.json")

# ----------------------------------------------------------------------------

ARXIV_API = "http://export.arxiv.org/api/query"
NS = {"atom": "http://www.w3.org/2005/Atom"}
PAGE_SIZE = 100
REQUEST_DELAY = 3.0        # arXiv asks for ~3s between requests
FETCH_ATTEMPTS = 3         # retries on 429/timeout, backing off 3s -> 6s
TELEGRAM_LIMIT = 4096      # Telegram's hard cap on a single message


def fetch_page(url):
    """
    One API request, retrying on transient failures.

    arXiv answers 429 when it thinks you are hammering it, and occasionally
    just times out. Back off and retry rather than treating either as a quiet
    week. Delays only grow - never retry faster than REQUEST_DELAY.
    """
    for attempt in range(FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read()
        except Exception as exc:
            if attempt == FETCH_ATTEMPTS - 1:
                raise
            wait = REQUEST_DELAY * (2 ** attempt)
            print(f"[warn] {exc} - retrying in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)


def fetch_recent():
    """
    Page through the arXiv API, newest first.

    Returns (papers, ok). `ok` is False if the very first page failed, which
    means we learned nothing at all - as opposed to a successful fetch that
    genuinely had nothing worth sending.
    """
    query = " OR ".join(f"cat:{c}" for c in CATEGORIES)
    papers = []

    for start in range(0, MAX_FETCH, PAGE_SIZE):
        params = urllib.parse.urlencode({
            "search_query": query,
            "start": start,
            "max_results": PAGE_SIZE,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        })
        url = f"{ARXIV_API}?{params}"

        try:
            raw = fetch_page(url)
        except Exception as exc:
            print(f"[error] fetch failed at start={start}: {exc}", file=sys.stderr)
            if start == 0:
                return [], False
            break       # partial results are still worth scoring

        entries = ET.fromstring(raw).findall("atom:entry", NS)
        if not entries:
            break

        papers.extend(parse_entry(e) for e in entries)

        if start + PAGE_SIZE < MAX_FETCH:
            time.sleep(REQUEST_DELAY)

    return [p for p in papers if p], True


def parse_entry(entry):
    """Turn one Atom <entry> into a plain dict."""
    def text(tag):
        node = entry.find(f"atom:{tag}", NS)
        return " ".join(node.text.split()) if node is not None and node.text else ""

    raw_id = text("id")
    if not raw_id:
        return None

    published = text("published")
    try:
        when = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None

    authors = [
        " ".join(n.text.split())
        for n in entry.findall("atom:author/atom:name", NS)
        if n.text
    ]

    paper_id = re.sub(r"v\d+$", "", raw_id.rsplit("/", 1)[-1])

    return {
        "id": paper_id,
        "title": text("title"),
        "abstract": text("summary"),
        "authors": authors,
        "published": when,
        "url": f"https://arxiv.org/abs/{paper_id}",
    }


def score(paper):
    """Weighted keyword match. Title hits count double."""
    title = paper["title"].lower()
    body = title + " " + paper["abstract"].lower()
    total = 0
    for kw, weight in KEYWORDS.items():
        if kw in body:
            total += weight
        if kw in title:
            total += weight
    return total


def load_state():
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        print("[warn] sent.json unreadable, starting fresh", file=sys.stderr)
        return []


def save_state(seen):
    STATE_FILE.write_text(json.dumps(seen[-500:], indent=2) + "\n")


def pick(papers, seen):
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    seen_set = set(seen)

    candidates = [
        p for p in papers
        if p["published"] >= cutoff and p["id"] not in seen_set
    ]
    for p in candidates:
        p["score"] = score(p)

    candidates = [p for p in candidates if p["score"] >= MIN_SCORE]
    candidates.sort(key=lambda p: (-p["score"], -p["published"].timestamp()))
    return candidates[:PAPERS_PER_RUN]


def render(papers):
    """Return (plain_text, html)."""
    lines, blocks = [], []

    for i, p in enumerate(papers, 1):
        authors = ", ".join(p["authors"][:4])
        if len(p["authors"]) > 4:
            authors += " et al."
        abstract = p["abstract"]
        if len(abstract) > 700:
            abstract = abstract[:700].rsplit(" ", 1)[0] + "..."

        lines.append(
            f"{i}. {p['title']}\n"
            f"   {authors}\n"
            f"   {p['url']}\n\n"
            f"   {abstract}\n"
        )
        # Abstracts carry raw LaTeX, so &, < and > show up routinely and would
        # otherwise swallow the rest of the message in an HTML client.
        blocks.append(
            f"<h3 style='margin-bottom:4px'>{i}. {html.escape(p['title'])}</h3>"
            f"<p style='margin:0;color:#555;font-size:14px'>{html.escape(authors)}</p>"
            f"<p style='margin:6px 0'>"
            f"<a href='{html.escape(p['url'])}'>{html.escape(p['url'])}</a></p>"
            f"<p style='line-height:1.5'>{html.escape(abstract)}</p>"
        )

    text = "\n".join(lines)
    body = (
        "<div style=\"font-family:-apple-system,Segoe UI,sans-serif;max-width:640px\">"
        + "<hr style='border:none;border-top:1px solid #ddd'>".join(blocks)
        + "</div>"
    )
    return text, body


def send_email(subject, text, html):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO") or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)

    print(f"[ok] emailed {to}")


def send_whatsapp(subject, papers):
    """
    WhatsApp via CallMeBot - free, no registration, personal use only.
    One-time setup: save +34 684 72 39 62 to your contacts, WhatsApp it
    "I allow callmebot to send me messages", and it replies with your API key.
    """
    phone = os.environ["WHATSAPP_PHONE"]        # e.g. +15551234567
    apikey = os.environ["WHATSAPP_APIKEY"]

    # WhatsApp messages want to be short - title and link only, no abstracts.
    lines = [f"*{subject}*", ""]
    for i, p in enumerate(papers, 1):
        title = p["title"]
        if len(title) > 110:
            title = title[:110].rsplit(" ", 1)[0] + "..."
        lines.append(f"{i}. {title}")
        lines.append(p["url"])
        lines.append("")

    params = urllib.parse.urlencode({
        "phone": phone,
        "text": "\n".join(lines),
        "apikey": apikey,
    })

    with urllib.request.urlopen(
        f"https://api.callmebot.com/whatsapp.php?{params}", timeout=30
    ) as resp:
        resp.read()

    print(f"[ok] WhatsApp sent to {phone}")


def send_telegram(subject, papers):
    """
    Telegram via a personal bot - official API, free, no per-message billing.
    One-time setup: message @BotFather, /newbot, and it hands back a token.
    Then message your new bot once so it is allowed to reply, and read the
    chat id out of https://api.telegram.org/bot<TOKEN>/getUpdates.

    Roomier than WhatsApp at 4096 characters, so a short abstract fits.
    """
    token = os.environ["TELEGRAM_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    parts = [f"<b>{html.escape(subject)}</b>", ""]
    for i, p in enumerate(papers, 1):
        abstract = p["abstract"]
        if len(abstract) > 300:
            abstract = abstract[:300].rsplit(" ", 1)[0] + "..."
        block = (
            f"{i}. <b>{html.escape(p['title'])}</b>\n"
            f"{html.escape(abstract)}\n"
            f"{html.escape(p['url'])}\n"
        )
        # Drop whole papers rather than character-clipping the tail: a cut
        # landing inside "&amp;" or a <b> tag makes Telegram reject the lot
        # with "can't parse entities". Two papers never come close anyway -
        # this only matters if PAPERS_PER_RUN is raised a lot.
        if sum(len(s) + 1 for s in parts) + len(block) > TELEGRAM_LIMIT:
            print(f"[warn] Telegram: {len(papers) - i + 1} paper(s) did not fit",
                  file=sys.stderr)
            break
        parts.append(block)

    message = "\n".join(parts)

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            resp.read()
    except Exception as exc:
        # The token sits in the URL path, and some urllib errors quote the URL
        # back. Scrub it rather than risk printing it into the Actions log.
        # `from None` drops the chained traceback, which quotes it too.
        raise RuntimeError(str(exc).replace(token, "<TELEGRAM_TOKEN>")) from None

    print(f"[ok] Telegram sent to chat {chat_id}")


def missing_vars(*names):
    """Env vars that are unset or empty. Actions supplies "" for absent secrets."""
    return [n for n in names if not os.environ.get(n)]


def deliver(subject, papers, text, body):
    """
    Use whichever channels have credentials configured.

    Each channel is isolated: a half-configured or failing one reports itself
    and lets the others still go out. Returns True if anything was delivered or
    printed, False if every configured channel failed - the caller uses that to
    decide whether these papers may be marked as sent.
    """
    attempted = False
    sent = False

    # (label, env var that switches it on, also-required vars, sender)
    channels = [
        ("email", "SMTP_USER", ("SMTP_HOST", "SMTP_PASS"),
         lambda: send_email(subject, text, body)),
        ("Telegram", "TELEGRAM_TOKEN", ("TELEGRAM_CHAT_ID",),
         lambda: send_telegram(subject, papers)),
        ("WhatsApp", "WHATSAPP_APIKEY", ("WHATSAPP_PHONE",),
         lambda: send_whatsapp(subject, papers)),
    ]

    for label, trigger, required, send in channels:
        if not os.environ.get(trigger):
            continue
        attempted = True

        # Names only - never the values.
        absent = missing_vars(*required)
        if absent:
            print(
                f"[error] {label} skipped, unset secret(s): {', '.join(absent)}",
                file=sys.stderr,
            )
            continue

        try:
            send()
            sent = True
        except Exception as exc:
            print(f"[error] {label} failed: {exc}", file=sys.stderr)

    if not attempted:
        print("[warn] no delivery credentials set - printing instead\n")
        print(text)
        return True

    if not sent:
        # Nothing got through. Print so the run is still useful, and let the
        # caller leave these papers unsent so next week retries them.
        print("[warn] every configured channel failed - printing instead\n")
        print(text)

    return sent


def main():
    seen = load_state()
    papers, ok = fetch_recent()

    if not ok:
        # Distinct from a quiet week - we never got to look. Go red so it is
        # visible, instead of a green run that silently sent nothing.
        print("[error] could not reach the arXiv API - giving up for now",
              file=sys.stderr)
        return 1

    print(f"[info] fetched {len(papers)} papers, {len(seen)} already sent")

    picks = pick(papers, seen)
    if not picks:
        print("[info] nothing new cleared the score threshold this week")
        return 0

    subject = f"arXiv digest - {datetime.now(timezone.utc):%b %d}"
    text, body = render(picks)

    if not deliver(subject, picks, text, body):
        # Leave sent.json alone so these papers come back around next week,
        # and fail loudly so the Actions run goes red instead of quietly red-
        # herringing a successful send.
        print("[error] nothing delivered - not recording these as sent",
              file=sys.stderr)
        return 1

    save_state(seen + [p["id"] for p in picks])
    print(f"[ok] sent {len(picks)}: " + ", ".join(p["id"] for p in picks))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
