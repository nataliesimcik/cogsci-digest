"""Weekly cognitive science digest: fetch -> screen -> summarize -> publish data for the site.

Usage:
  python digest.py              # full run (needs ANTHROPIC_API_KEY)
  python digest.py --dry-run    # fetch only, print counts, no Claude calls
  python digest.py --days 14    # override lookback window
"""
import argparse
import json
import re
from datetime import date, timedelta
from email.utils import format_datetime
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import sources
from common import ROOT, DATA, load_config, parse_json, ask, write_json

SEEN_FILE = ROOT / "data" / "seen.json"
SEEN_KEEP_DAYS = 90


def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen, today):
    cutoff = (today - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    write_json(SEEN_FILE, {k: v for k, v in seen.items() if v >= cutoff})


# ---------- fetch ----------
def collect(cfg, since, today):
    s = cfg["sources"]
    papers = []
    print("Fetching…")
    papers += sources.fetch_crossref(s.get("journals", []), since, cfg["contact_email"])
    papers += sources.fetch_arxiv(s.get("arxiv_queries", []), since)
    if s.get("psyarxiv"):
        papers += sources.fetch_psyarxiv(since)
    if s.get("biorxiv_categories"):
        papers += sources.fetch_biorxiv(s["biorxiv_categories"], since, today)
    by_id, titles = {}, set()
    for p in papers:
        key = re.sub(r"\W+", "", p["title"].lower())
        if not p["title"] or p["id"] in by_id or key in titles:
            continue
        by_id[p["id"]] = p
        titles.add(key)
    return list(by_id.values())


# ---------- screen ----------
SCREEN_SYSTEM = """You screen new academic papers for a cognitive science lab's weekly digest.
For each paper:
- score 0-10: fit with the reader's interests and how substantive it looks
  (10 = squarely on-topic and likely important; 0 = off-topic or not a research paper).
- topics: 1-3 keys from the allowed list, most central first.
Reply with ONLY a JSON array covering every paper:
[{"id": <int>, "score": <int>, "topics": ["key", ...]}]"""


def screen(client, cfg, papers, batch_size=40):
    allowed = cfg["topics"]
    topic_list = "\n".join(f"- {k}: {v}" for k, v in allowed.items())
    for i in range(0, len(papers), batch_size):
        batch = papers[i:i + batch_size]
        listing = "\n\n".join(
            f"[{i + j}] {p['title']}\nVenue: {p['venue']}\n"
            f"Abstract: {p['abstract'][:1200] or '(not available)'}"
            for j, p in enumerate(batch))
        user = (f"Reader's interests:\n{cfg['interests']}\n\nAllowed topic keys:\n{topic_list}"
                f"\n\nPapers:\n{listing}")
        try:
            results = parse_json(ask(client, cfg["models"]["screen"], SCREEN_SYSTEM, user))
        except Exception as e:
            print(f"  ! screening batch {i}: {e}")
            continue
        for r in results:
            idx = int(r.get("id", -1))
            if i <= idx < i + len(batch):
                papers[idx]["score"] = int(r.get("score", 0))
                papers[idx]["topics"] = [t for t in r.get("topics", []) if t in allowed][:3]
        print(f"  screened {min(i + batch_size, len(papers))}/{len(papers)}")
    return [p for p in papers if "score" in p]


# ---------- summarize ----------
SUMMARY_SYSTEM = """You write summaries of new research papers for cognitive scientists.
For each paper write 2-3 sentences: what was done, what was found, why it matters.
Precise, plain prose, no hype. Base every claim strictly on the title and abstract;
never invent findings, sample sizes or effect sizes. If no abstract is available,
write one sentence saying what the title indicates the paper is about.
Reply with ONLY a JSON array: [{"id": <int>, "summary": "..."}]"""

OVERVIEW_SYSTEM = """You write the opening of a weekly research digest for a cognitive science
lab. Given this week's top papers, write 3-5 sentences on the notable threads and
standout results. Plain prose, no headings, no hype, no paper titles in quotes;
refer to work by its finding or topic. Base claims only on the summaries given."""


def summarize(client, cfg, papers, batch_size=15):
    for i in range(0, len(papers), batch_size):
        batch = papers[i:i + batch_size]
        listing = "\n\n".join(
            f"[{i + j}] {p['title']}\nAbstract: {p['abstract'][:2500] or '(not available)'}"
            for j, p in enumerate(batch))
        try:
            results = parse_json(ask(client, cfg["models"]["write"], SUMMARY_SYSTEM, listing))
        except Exception as e:
            print(f"  ! summary batch {i}: {e}")
            continue
        for r in results:
            idx = int(r.get("id", -1))
            if i <= idx < i + len(batch):
                papers[idx]["summary"] = r.get("summary", "").strip()
        print(f"  summarized {min(i + batch_size, len(papers))}/{len(papers)}")


def overview(client, cfg, papers):
    tops = [p for p in papers if p.get("highlight")] or papers[:8]
    text = "\n\n".join(f"- {p['title']}: {p.get('summary', '')}" for p in tops)
    try:
        return ask(client, cfg["models"]["write"], OVERVIEW_SYSTEM, text, max_tokens=800).strip()
    except Exception as e:
        print(f"  ! overview: {e}")
        return ""


# ---------- publish ----------
FIELDS = ("id", "title", "authors", "venue", "url", "date", "score", "topics", "summary", "highlight")


def publish(cfg, today, since, papers, intro, fetched):
    week = {
        "week": today.isoformat(), "since": since.isoformat(), "until": today.isoformat(),
        "fetched": fetched, "overview": intro,
        "papers": [{k: p.get(k) for k in FIELDS} for p in papers],
    }
    write_json(DATA / "weeks" / f"{today.isoformat()}.json", week)

    weeks = sorted((p.stem for p in (DATA / "weeks").glob("*.json")), reverse=True)
    write_json(DATA / "index.json", {
        "title": cfg["title"], "repo_url": cfg.get("repo_url", ""),
        "topics": [{"key": k, "label": v} for k, v in cfg["topics"].items()],
        "weeks": weeks,
    })
    write_feed(cfg, weeks)
    print(f"Published week {today.isoformat()}: {len(papers)} papers")


def write_feed(cfg, weeks):
    site = cfg.get("site_url", "").rstrip("/") + "/"
    items = []
    for w in weeks[:20]:
        data = json.loads((DATA / "weeks" / f"{w}.json").read_text())
        tops = [p for p in data["papers"] if p.get("highlight")][:8]
        body = f"<p>{escape(data.get('overview') or '')}</p><ul>" + "".join(
            f'<li><a href="{escape(p["url"])}">{escape(p["title"])}</a></li>' for p in tops
        ) + f"</ul><p>{len(data['papers'])} papers this week.</p>"
        pub = format_datetime(datetime.fromisoformat(w).replace(hour=7, tzinfo=timezone.utc))
        items.append(
            f"<item><title>{escape(cfg['title'])}: week of {w}</title>"
            f"<link>{site}#week={w}</link><guid isPermaLink=\"false\">{w}</guid>"
            f"<pubDate>{pub}</pubDate><description>{escape(body)}</description></item>")
    (ROOT / "docs" / "feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        f"<title>{escape(cfg['title'])}</title><link>{site}</link>"
        "<description>Weekly papers and preprints in cognitive science</description>"
        + "".join(items) + "</channel></rss>\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int)
    args = ap.parse_args()

    cfg = load_config()
    today = date.today()
    since = today - timedelta(days=args.days or cfg.get("lookback_days", 7))

    seen = load_seen()
    papers = [p for p in collect(cfg, since, today) if p["id"] not in seen]
    print(f"{len(papers)} new items after dedupe")
    sources.enrich_abstracts(papers, cfg["contact_email"])
    if args.dry_run:
        for p in papers[:15]:
            print(f"  - [{p['venue']}] {p['title'][:100]}")
        return

    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    scored = screen(client, cfg, papers) if papers else []
    relevant = sorted((p for p in scored if p["score"] >= cfg["min_relevance"]),
                      key=lambda p: p["score"], reverse=True)[:cfg["max_papers"]]
    for k, p in enumerate(relevant):
        p["highlight"] = k < cfg["top_picks"]
    if relevant:
        summarize(client, cfg, relevant)
    intro = overview(client, cfg, relevant) if relevant else ""

    publish(cfg, today, since, relevant, intro, len(papers))
    for p in papers:
        seen[p["id"]] = today.isoformat()
    save_seen(seen, today)


if __name__ == "__main__":
    main()
