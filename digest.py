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
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

# ----------------------------------------------------------------------------
# CONFIG - edit this block, everything else can stay as is
# ----------------------------------------------------------------------------

CATEGORIES = ["cs.CV", "cs.AI", "cs.LG", "cs.CL"]

# Keyword -> weight. Matched against lowercased title + abstract.
# Title matches count double automatically.
KEYWORDS = {
    # world models
    "world model": 8,
    "intuitive physics": 6,
    "physical reasoning": 5,
    "physically grounded": 5,
    "jepa": 5,
    "predictive world": 5,
    "learned simulator": 4,
    "dynamics model": 4,
    # video LLMs
    "video language model": 8,
    "video-language model": 8,
    "video llm": 8,
    "video-llm": 8,
    "multimodal llm": 4,
    "video question answering": 4,
    # motion understanding
    "motion understanding": 8,
    "video understanding": 5,
    "temporal reasoning": 5,
    "video reasoning": 5,
    "long-horizon": 4,
    "spatiotemporal": 3,
    "temporal grounding": 3,
    "object permanence": 4,
    "counterfactual": 3,
    "motion": 2,
    "embodied": 2,
    "video": 1,
}

# "world model" alone drags in web agents, RL planners and LLM reasoning work.
# A paper has to be about video/motion at all to count, so require one of these.
REQUIRE_ANY = ["video", "motion", "frame", "temporal", "dynamic", "visual",
               "physical", "spatial"]

# arXiv puts acceptances in <arxiv:comment>/<arxiv:journal_ref> when they exist,
# which for brand-new preprints is usually not yet. Treated as a bonus, never a
# filter - see README, gating on it sends nothing.
VENUE_RE = re.compile(
    r"\b(neurips|nips|iclr|icml|cvpr|eccv|iccv|acl|emnlp|naacl|tpami|siggraph)\b",
    re.I,
)
VENUE_BONUS = 6

# Drop anything scoring below this, even if it's the best of a weak week.
# Tuned against a 600-paper sample: 16 yields ~12 candidates a week, so there
# is real competition for the 2 slots rather than "whatever cleared the bar".
MIN_SCORE = 16

PAPERS_PER_RUN = 1         # 1 per run x Mon/Fri = 2 a week
SHORTLIST = 12             # top-scoring candidates handed to the novelty judge

# Which model does the judging, when a key for it exists. Override either with
# an env var of the same name if you want to change model without editing code.
CLAUDE_MODEL = "claude-opus-5"
GEMINI_MODEL = "gemini-flash-latest"   # resolved against the live API; the
                                       # "-latest" alias tracks Google's
                                       # renames instead of going stale

# Fallback when no LLM key is set. These score how a paper is *written*, which
# is a weak proxy for whether the idea is new - abstracts are written to sound
# novel. Good enough to break ties, not good enough to trust on its own.
NOVELTY_HINTS = {
    "we introduce": 3, "we propose a new": 3, "for the first time": 4,
    "first work": 4, "rethinking": 3, "we challenge": 4, "counterintuitive": 4,
    "surprisingly": 3, "emerges": 3, "new paradigm": 4, "fundamentally": 3,
    "we show that": 2, "unlike prior": 2, "paradigm shift": 4,
}
INCREMENTAL_HINTS = {
    "state-of-the-art": -3, "outperforms": -3, "we improve": -3,
    "extends": -2, "fine-tun": -2, "we adapt": -2, "building upon": -3,
    "simple modification": -3, "achieves competitive": -3,
    "benchmark results show": -2, "extensive experiments": -2,
}
LOOKBACK_DAYS = 8          # comfortably covers the Mon/Fri gaps (3 and 4 days),
                           # so a slow stretch still has a pool to draw from.
                           # Overlap between runs is fine - dedupe handles it.
MAX_FETCH = 400            # how many recent papers to consider
STATE_FILE = Path("sent.json")

# ----------------------------------------------------------------------------

ARXIV_API = "https://export.arxiv.org/api/query"

# arXiv throttles hard on the default "Python-urllib/3.12" - it is what every
# scraper sends - and shared CI egress IPs make that worse. Identifying the
# tool is what arXiv asks for, and is the difference between 200 and 429.
USER_AGENT = "arxiv-digest/1.0 (+https://github.com/shahidkamal-ml/arxiv-digest)"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",   # comment, journal_ref
}
PAGE_SIZE = 100
REQUEST_DELAY = 3.0        # arXiv asks for ~3s between requests
FETCH_ATTEMPTS = 5         # retries on 429/timeout: 3s -> 6s -> 12s -> 24s
MAX_BACKOFF = 60.0         # ceiling per wait, including any Retry-After
TELEGRAM_LIMIT = 4096      # Telegram's hard cap on a single message


def fetch_page(url):
    """
    One API request, retrying on transient failures.

    arXiv answers 429 when it thinks you are hammering it, and occasionally
    just times out. Back off and retry rather than treating either as a quiet
    week. Delays only grow - never retry faster than REQUEST_DELAY.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.read()
        except Exception as exc:
            if attempt == FETCH_ATTEMPTS - 1:
                raise

            wait = min(REQUEST_DELAY * (2 ** attempt), MAX_BACKOFF)

            # On a 429 arXiv may say exactly how long to wait. Prefer that over
            # our guess - backing off too little is what keeps you throttled.
            retry_after = getattr(exc, "headers", None)
            if retry_after is not None:
                try:
                    wait = max(wait, min(float(retry_after.get("Retry-After")),
                                         MAX_BACKOFF))
                except (TypeError, ValueError):
                    pass

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
    def text(tag, ns="atom"):
        node = entry.find(f"{ns}:{tag}", NS)
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
        # Where an acceptance is announced, when the authors bother to say.
        "venue": (text("comment", "arxiv") + " " + text("journal_ref", "arxiv")).strip(),
    }


def on_topic(paper):
    """Is this about video/motion at all, or just a world model of something?"""
    body = (paper["title"] + " " + paper["abstract"]).lower()
    return any(term in body for term in REQUIRE_ANY)


def score(paper):
    """Weighted keyword match. Title hits count double, acceptances get a bonus."""
    title = paper["title"].lower()
    body = title + " " + paper["abstract"].lower()
    total = 0
    for kw, weight in KEYWORDS.items():
        if kw in body:
            total += weight
        if kw in title:
            total += weight

    if paper.get("venue") and VENUE_RE.search(paper["venue"]):
        total += VENUE_BONUS

    return total


JUDGE_PROMPT = """\
You are triaging new arXiv preprints for a researcher who works on world \
models, video LLMs, and motion understanding from video.

They want genuinely new ideas. They do NOT want competent but incremental \
work: a new benchmark number, another architecture tweak, a scaled-up \
rerun, or a fine-tune of an existing model. Abstracts are written to sound \
novel, so judge the actual claim, not the adjectives. Be sceptical - most \
papers are incremental, and saying so is the useful answer.

Rank these {n} papers, most genuinely novel first.

{papers}

Reply with one line per paper, best first, in exactly this format:
NUMBER|one sentence on what is actually new, or why it is incremental

No other text."""


def judge_prompt(papers):
    blocks = []
    for i, p in enumerate(papers, 1):
        abstract = p["abstract"]
        if len(abstract) > 1200:
            abstract = abstract[:1200].rsplit(" ", 1)[0] + "..."
        blocks.append(f"[{i}] {p['title']}\n{abstract}")
    return JUDGE_PROMPT.format(n=len(papers), papers="\n\n".join(blocks))


def post_json(url, payload, headers, timeout=90):
    """POST JSON, return parsed JSON. Keys travel in headers, never the URL."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", **headers},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read())


GEMINI_ROOT = "https://generativelanguage.googleapis.com/v1beta"


def gemini_generate(model, prompt, key):
    data = post_json(
        f"{GEMINI_ROOT}/models/{model}:generateContent",
        {"contents": [{"parts": [{"text": prompt}]}]},
        {"x-goog-api-key": key},
    )
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


def resolve_gemini_model(key):
    """
    Ask the API which models this key can actually use.

    Google retires and renames these often enough that a hardcoded id goes
    stale silently - it 404s and the digest quietly drops to heuristics. Rather
    than guess, list what the key has and prefer a cheap stable flash model.
    """
    request = urllib.request.Request(
        f"{GEMINI_ROOT}/models", headers={"x-goog-api-key": key}
    )
    with urllib.request.urlopen(request, timeout=30) as resp:
        models = json.loads(resp.read()).get("models", [])

    usable = [
        m["name"].split("/", 1)[-1] for m in models
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    stable = [m for m in usable if not re.search(r"preview|exp|thinking", m)]

    for want in ("flash-latest", "flash"):
        for name in sorted(stable or usable, reverse=True):
            if want in name:
                return name
    return (stable or usable or [None])[0]


def ask_gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("GEMINI_MODEL") or GEMINI_MODEL
    try:
        return gemini_generate(model, prompt, key)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        resolved = resolve_gemini_model(key)
        if not resolved:
            raise RuntimeError("no Gemini model available to this key") from None
        print(f"[info] {model} unavailable, using {resolved}", file=sys.stderr)
        return gemini_generate(resolved, prompt, key)


def ask_claude(prompt):
    key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("CLAUDE_MODEL") or CLAUDE_MODEL
    data = post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
        },
        {"x-api-key": key, "anthropic-version": "2023-06-01"},
    )
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("Claude declined to answer")
    # content is a list of blocks. Thinking is on by default on current models,
    # so content[0] is usually a thinking block - take the text ones.
    return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")


def parse_ranking(reply, count):
    """Pull '3|reason' lines out of the reply. Ignores anything malformed."""
    order = []
    for line in reply.splitlines():
        match = re.match(r"\s*\[?(\d+)\]?\s*[|.:\-]\s*(.+)", line)
        if not match:
            continue
        idx = int(match.group(1)) - 1
        if 0 <= idx < count and idx not in [i for i, _ in order]:
            order.append((idx, match.group(2).strip()))
    return order


def heuristic_novelty(paper):
    body = (paper["title"] + " " + paper["abstract"]).lower()
    return (sum(w for kw, w in NOVELTY_HINTS.items() if kw in body)
            + sum(w for kw, w in INCREMENTAL_HINTS.items() if kw in body))


def rank(papers):
    """
    Order by how genuinely novel the work looks, best first.

    Tries whichever LLM has a key, then falls back to phrase heuristics. A
    judge that errors, gets rate-limited or returns garbage must never take the
    digest down - it just means a slightly worse ordering this run.
    """
    if len(papers) < 2:
        return papers

    judges = [("Gemini", "GEMINI_API_KEY", ask_gemini),
              ("Claude", "ANTHROPIC_API_KEY", ask_claude)]

    for label, key_var, ask in judges:
        if not os.environ.get(key_var):
            continue
        try:
            order = parse_ranking(ask(judge_prompt(papers)), len(papers))
            if not order:
                raise ValueError("no parseable ranking in the reply")
        except Exception as exc:
            print(f"[warn] {label} judge failed ({exc}) - trying next",
                  file=sys.stderr)
            continue

        ranked = []
        for idx, reason in order:
            papers[idx]["reason"] = reason
            ranked.append(papers[idx])
        # Anything the judge silently dropped keeps its keyword order at the back.
        ranked += [p for p in papers if p not in ranked]
        print(f"[ok] ranked by {label}")
        return ranked

    configured = any(os.environ.get(v) for _, v, _ in judges)
    print("[info] " + ("every judge failed" if configured else "no LLM key set")
          + " - ranking by phrase heuristics", file=sys.stderr)
    return sorted(papers, key=lambda p: -(p["score"] + heuristic_novelty(p)))


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
        if p["published"] >= cutoff and p["id"] not in seen_set and on_topic(p)
    ]
    for p in candidates:
        p["score"] = score(p)

    candidates = [p for p in candidates if p["score"] >= MIN_SCORE]
    candidates.sort(key=lambda p: (-p["score"], -p["published"].timestamp()))

    # Keywords decide what is on topic; the judge decides which is worth reading.
    shortlist = candidates[:SHORTLIST]
    print(f"[info] {len(shortlist)} candidates cleared MIN_SCORE={MIN_SCORE}")
    return rank(shortlist)[:PAPERS_PER_RUN]


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

        why = p.get("reason", "")
        lines.append(
            f"{i}. {p['title']}\n"
            f"   {authors}\n"
            f"   {p['url']}\n"
            + (f"\n   Why this one: {why}\n" if why else "")
            + f"\n   {abstract}\n"
        )
        # Abstracts carry raw LaTeX, so &, < and > show up routinely and would
        # otherwise swallow the rest of the message in an HTML client.
        blocks.append(
            f"<h3 style='margin-bottom:4px'>{i}. {html.escape(p['title'])}</h3>"
            f"<p style='margin:0;color:#555;font-size:14px'>{html.escape(authors)}</p>"
            f"<p style='margin:6px 0'>"
            f"<a href='{html.escape(p['url'])}'>{html.escape(p['url'])}</a></p>"
            + (f"<p style='margin:6px 0;padding:8px 12px;background:#f4f6f8;"
               f"border-left:3px solid #888;line-height:1.45'>"
               f"<b>Why this one:</b> {html.escape(why)}</p>" if why else "")
            + f"<p style='line-height:1.5'>{html.escape(abstract)}</p>"
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
    raw_port = os.environ.get("SMTP_PORT") or "465"
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to = os.environ.get("EMAIL_TO") or user

    try:
        port = int(raw_port.strip())
    except ValueError:
        # Actions masks secret values everywhere, so the bad value shows up as
        # *** and the stock ValueError is unreadable. Say what is wrong instead.
        raise RuntimeError(
            "SMTP_PORT must be a number like 465 - the secret's value is not "
            "one (a masked *** here usually means the secret name got pasted "
            "into the value box)"
        ) from None

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
