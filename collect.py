"""Weekly feed collector.

Collects one week of AI news and papers, India finance items and matching
jobs, and writes them to latest.md (plus a copy in archive/). It runs on
GitHub Actions once a week. It calls no AI and needs no API keys.

To change what it collects, edit the SETTINGS section below. Everything
after that section is the machinery; you should not need to touch it.
"""

import datetime as dt
import email.utils
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# =====================================================================
# SETTINGS: edit these
# =====================================================================

DAYS = 7            # how far back to look for news, papers and finance
JOB_DAYS = 14       # how far back to look for new job posts
MAX_PER_SECTION = 10
MAX_JOBS_PER_COMPANY = 5

# Job titles must contain at least one of these words (lower case).
JOB_TITLE_WORDS = [
    "operations", " ops", "crm", "automation", "revops", "workflow",
    "analyst", "associate", "coordinator", "specialist", "apprentice",
    "support representative", "customer support", "customer success",
    "onboarding", "implementation", "trainee", "graduate", "new grad",
]

# Job titles with any of these words are skipped (too senior or not your track).
JOB_TITLE_SKIP = [
    "engineer", "developer", "architect", "scientist", "director", "head of",
    "principal", "staff ", "senior", "sr.", "sr ", "lead", "manager", "vp",
    "president", "counsel", "attorney", "designer",
]

# A job is kept if its location mentions one of these...
PLACE_OK = ["india", "bengaluru", "bangalore", "karnataka"]
# ...or if it is remote and does not limit itself to one of these regions.
REMOTE_WORDS = ["remote", "anywhere", "worldwide", "home based", "distributed"]
REGION_LIMITS = [
    "united states", "usa", "us", "u.s.", "north america", "canada", "americas", "amer",
    "namer", "latam", "emea", "europe", "eu", "united kingdom", "uk",
    "germany", "france", "spain", "australia", "brazil", "mexico",
]

# Company watchlist: (job board, board name, name to show).
# Board name = the part after the board's address, for example
# jobs.lever.co/gohighlevel -> "gohighlevel".
COMPANIES = [
    # Your picks
    ("greenhouse", "anthropic", "Anthropic"),
    ("ashby", "openai", "OpenAI"),
    # Remote-first or remote from India
    ("lever", "gohighlevel", "HighLevel"),
    ("ashby", "zapier", "Zapier"),
    ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "canonical", "Canonical"),
    ("greenhouse", "twilio", "Twilio"),
    ("greenhouse", "elastic", "Elastic"),
    # Offices in Bengaluru
    ("greenhouse", "stripe", "Stripe"),
    ("greenhouse", "karya", "Karya"),
    ("lever", "hevodata", "Hevo Data"),
    # Workable boards: names taken from their careers pages, not yet tested
    ("workable", "laundryheap-2", "Laundryheap"),
    ("workable", "lokal-app", "Lokal"),
]

# News feeds (RSS or Atom).
AI_FEEDS = [
    ("Hacker News, 150+ points",
     "https://hnrss.org/newest?q=AI+OR+LLM+OR+GPT+OR+Claude+OR+Gemini&points=150"),
    ("Google News: AI models",
     "https://news.google.com/rss/search?q=%22AI+model%22+launch+when:7d&hl=en-IN&gl=IN&ceid=IN:en"),
]
FINANCE_FEEDS = [
    ("RBI press releases", "https://rbi.org.in/pressreleases_rss.xml"),
    ("SEBI", "https://www.sebi.gov.in/sebirss.xml"),
    ("Google News: Indian markets",
     "https://news.google.com/rss/search?q=Sensex+Nifty+week+when:7d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("Google News: RBI policy",
     "https://news.google.com/rss/search?q=RBI+repo+rate+OR+%22monetary+policy%22+when:7d&hl=en-IN&gl=IN&ceid=IN:en"),
]
# RBI and SEBI post many routine notices; keep only titles with these words.
FINANCE_KEEP = [
    "repo", "monetary policy", "inflation", "policy rate", "alert",
    "fraud", "caution", "investor", "warns", "press release", "governor",
    "consumer", "digital", "upi", "unregistered", "statement on",
]
FINANCE_SKIP = ["money market operations", "auction", "results of"]

INDEXES = [("Nifty 50", "^NSEI"), ("Sensex", "^BSESN")]

REMOTIVE_URL = "https://remotive.com/api/remote-jobs?limit=300"
HF_PAPERS_URL = "https://huggingface.co/api/daily_papers"
HN_HIRING_FEED = "https://hnrss.org/whoishiring"

# =====================================================================
# Machinery
# =====================================================================

USER_AGENT = "weekly-feeds/1.0 (personal weekly digest; runs once a week)"
NOW = dt.datetime.now(dt.timezone.utc)
SINCE = NOW - dt.timedelta(days=DAYS)
JOB_SINCE = NOW - dt.timedelta(days=JOB_DAYS)
problems = []


def fetch(url, tries=2):
    """Download a URL and return its bytes, or None after noting the problem."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as exc:  # network errors, 4xx, 5xx
            if attempt + 1 == tries:
                problems.append(f"{url} ({exc})")
                return None
            time.sleep(3)


def fetch_json(url):
    data = fetch(url)
    if data is None:
        return None
    try:
        return json.loads(data)
    except ValueError as exc:
        problems.append(f"{url} (not JSON: {exc})")
        return None


def parse_date(text):
    """Read RSS, ISO or millisecond dates; return an aware datetime or None."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        return dt.datetime.fromtimestamp(text / 1000, dt.timezone.utc)
    text = str(text).strip()
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def read_feed(url):
    """Return [(title, link, date)] from an RSS or Atom feed."""
    data = fetch(url)
    if data is None:
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        problems.append(f"{url} (could not read the feed: {exc})")
        return []
    items = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        title = link = date = None
        for child in node:
            name = local(child.tag)
            if name == "title":
                title = clean(child.text)
            elif name == "link":
                link = (child.get("href") or child.text or "").strip()
            elif name in ("pubdate", "published", "updated", "date") and date is None:
                date = parse_date(child.text)
        if title and link:
            items.append((title, link, date))
    return items


def recent(items, since):
    """Keep items newer than `since`; undated items are kept and marked."""
    return [i for i in items if i[2] is None or i[2] >= since]


def day(d):
    return d.strftime("%d %b %Y") if d else "date not shown"


def bullet(title, link, date, extra=""):
    extra = f" · {extra}" if extra else ""
    return f"- [{title}]({link}) · {day(date)}{extra}"


def has_any(text, words):
    text = f" {text.lower()} "
    return any(w in text for w in words)


# ---------- sections ----------

def section_feeds(feeds, keep=None, skip=None):
    lines = []
    for name, url in feeds:
        items = recent(read_feed(url), SINCE)
        if keep:
            items = [i for i in items if has_any(i[0], keep)]
        if skip:
            items = [i for i in items if not has_any(i[0], skip)]
        lines.append(f"\n### {name}\n")
        if items:
            lines += [bullet(*i) for i in items[:MAX_PER_SECTION]]
        else:
            lines.append("- nothing this week, or not available (see Problems)")
    return lines


def section_papers():
    seen = {}
    for back in range(DAYS):
        date = (NOW - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        data = fetch_json(f"{HF_PAPERS_URL}?date={date}")
        for entry in data or []:
            paper = entry.get("paper", entry)
            pid = paper.get("id")
            if not pid or pid in seen:
                continue
            seen[pid] = (
                clean(paper.get("title") or entry.get("title")),
                f"https://huggingface.co/papers/{pid}",
                parse_date(entry.get("publishedAt") or paper.get("publishedAt")),
                paper.get("upvotes", 0) or 0,
            )
    top = sorted(seen.values(), key=lambda p: p[3], reverse=True)[:5]
    if not top:
        return ["- not available (see Problems)"]
    return [bullet(t, l, d, f"{u} upvotes") for t, l, d, u in top]


def section_indexes():
    try:
        import yfinance as yf
    except ImportError:
        problems.append("yfinance is not installed")
        return ["- not available"]
    lines = []
    for name, symbol in INDEXES:
        try:
            closes = yf.Ticker(symbol).history(period="1mo")["Close"].dropna()
            if closes.empty:
                raise ValueError("no prices returned")
            last_day = closes.index[-1]
            week_ago = closes[closes.index <= last_day - dt.timedelta(days=7)]
            if week_ago.empty:
                raise ValueError("not enough data")
            last, before = float(closes.iloc[-1]), float(week_ago.iloc[-1])
            change = (last - before) / before * 100
            lines.append(
                f"- {name}: {last:,.0f} on {last_day:%d %b %Y}, "
                f"{change:+.1f}% from {before:,.0f} a week earlier"
            )
        except Exception as exc:
            problems.append(f"{name} ({symbol}) via yfinance ({exc})")
            lines.append(f"- {name}: not available")
    return lines


def mentions(text, words):
    """True if any word or phrase appears as a whole word in text."""
    text = text.lower()
    return any(re.search(r"(?<![a-z])" + re.escape(w.strip()) + r"(?![a-z])", text)
               for w in words)


def place_ok(location, remote_flag=False):
    if mentions(location, PLACE_OK):
        return "India"
    if remote_flag or mentions(location, REMOTE_WORDS):
        if not mentions(location, REGION_LIMITS):
            return "remote, check you can apply from India"
    return None


def title_ok(title):
    return has_any(title, JOB_TITLE_WORDS) and not has_any(title, JOB_TITLE_SKIP)


def jobs_greenhouse(board):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
    for job in (data or {}).get("jobs", []):
        yield (job.get("title", ""), (job.get("location") or {}).get("name", ""),
               job.get("absolute_url", ""),
               parse_date(job.get("first_published") or job.get("updated_at")), False)


def jobs_lever(board):
    data = fetch_json(f"https://api.lever.co/v0/postings/{board}?mode=json")
    for job in data or []:
        cats = job.get("categories") or {}
        places = cats.get("allLocations") or [cats.get("location", "")]
        loc = "; ".join(p for p in places if p)
        yield (job.get("text", ""), loc, job.get("hostedUrl", ""),
               parse_date(job.get("createdAt")),
               job.get("workplaceType") == "remote")


def jobs_ashby(board):
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    for job in (data or {}).get("jobs", []):
        if job.get("isListed") is False:
            continue
        extra = [s.get("location", "") for s in job.get("secondaryLocations") or []]
        loc = "; ".join([job.get("location", "")] + extra)
        yield (job.get("title", ""), loc, job.get("jobUrl", ""),
               parse_date(job.get("publishedAt")), bool(job.get("isRemote")))


def jobs_workable(board):
    data = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{board}")
    for job in (data or {}).get("jobs", []):
        places = job.get("locations") or [job]
        loc = "; ".join(
            ", ".join(x for x in (p.get("city"), p.get("country")) if x) for p in places
        )
        yield (job.get("title", ""), loc, job.get("url") or job.get("shortlink", ""),
               parse_date(job.get("published_on") or job.get("created_at")),
               bool(job.get("telecommuting")))


BOARDS = {"greenhouse": jobs_greenhouse, "lever": jobs_lever,
          "ashby": jobs_ashby, "workable": jobs_workable}


def section_company_jobs():
    lines, older = [], 0
    for platform, board, name in COMPANIES:
        found = []
        for title, loc, url, date, remote in BOARDS[platform](board):
            where = place_ok(loc, remote)
            if not (title and url and where and title_ok(title)):
                continue
            if date and date < JOB_SINCE:
                older += 1
                continue
            found.append((date or NOW, bullet(clean(title), url, date, f"{name} · {loc or where}")))
        found.sort(key=lambda f: f[0], reverse=True)
        lines += [f[1] for f in found[:MAX_JOBS_PER_COMPANY]]
    if not lines:
        lines.append(f"- no new matching jobs in the last {JOB_DAYS} days")
    lines.append(f"- Older matching jobs still open on these boards: {older}")
    return lines


def section_remote_boards():
    lines = []
    data = fetch_json(REMOTIVE_URL)
    for job in (data or {}).get("jobs", []):
        title = job.get("title", "")
        where = job.get("candidate_required_location", "") or ""
        date = parse_date(job.get("publication_date"))
        if not title_ok(title) or (date and date < JOB_SINCE):
            continue
        if not (has_any(where, PLACE_OK) or has_any(where, ["worldwide", "anywhere", "asia", "apac"])):
            continue
        lines.append(bullet(clean(title), job.get("url", ""), date,
                            f"{job.get('company_name', '')} · {where}"))
    lines = lines[:MAX_PER_SECTION] or ["- nothing matching on Remotive this week"]
    hiring = read_feed(HN_HIRING_FEED)
    if hiring:
        lines.append(bullet(*hiring[0], "Hacker News monthly hiring thread"))
    return lines


def main():
    year, week, _ = NOW.isocalendar()
    stamp = f"{year}-W{week:02d}"
    out = [
        f"# Weekly feeds, {stamp}",
        "",
        f"Collected {NOW:%Y-%m-%d %H:%M} UTC by collect.py. News and papers: last {DAYS} days. "
        f"Jobs: posted in the last {JOB_DAYS} days. Links only; nothing here is checked by a person.",
        "",
        "## AI news",
        *section_feeds(AI_FEEDS),
        "",
        "## AI papers (Hugging Face daily papers, top 5 by upvotes)",
        "",
        *section_papers(),
        "",
        "## India finance",
        "",
        "### Index moves",
        "",
        *section_indexes(),
        *section_feeds(FINANCE_FEEDS[:2], keep=FINANCE_KEEP, skip=FINANCE_SKIP),
        *section_feeds(FINANCE_FEEDS[2:]),
        "",
        "## Jobs: company watchlist",
        "",
        *section_company_jobs(),
        "",
        "## Jobs: remote boards",
        "",
        *section_remote_boards(),
        "",
        "## Problems this run",
        "",
        *([f"- {p}" for p in problems] or ["- none"]),
        "",
    ]
    text = "\n".join(out)
    Path("latest.md").write_text(text, encoding="utf-8")
    Path("archive").mkdir(exist_ok=True)
    Path("archive", f"{stamp}.md").write_text(text, encoding="utf-8")
    print(f"Wrote latest.md and archive/{stamp}.md with {len(problems)} problem(s).")


if __name__ == "__main__":
    sys.exit(main())
