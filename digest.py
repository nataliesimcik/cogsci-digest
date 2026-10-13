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
import suggestions
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
refer to work by its finding or topic. Base claims only on the summaries given.

Each paper has a number in square brackets. Whenever a sentence mentions specific
work, turn the words describing it into a link to its number, like
[an account of planning as resource-rational](#4). For several papers at once use
[two studies of serial dependence](#2,7). Link descriptive phrases, never words like
"this paper" or "here". Only use numbers from the list."""


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


LINK = re.compile(r"\[([^\]]+)\]\(#p?([\d,\s]+)\)")


def overview(client, cfg, papers):
    """Overview text. Links to papers are stored as [phrase](#p3,7), where the numbers
    are positions in the published paper list, so the site can scroll to them."""
    tops = [i for i, p in enumerate(papers) if p.get("highlight") or p.get("suggested_by")] \
        or list(range(min(8, len(papers))))
    text = "\n\n".join(f"[{i}] {papers[i]['title']}: {papers[i].get('summary', '')}" for i in tops)
    try:
        out = ask(client, cfg["models"]["write"], OVERVIEW_SYSTEM, text, max_tokens=900).strip()
    except Exception as e:
        print(f"  ! overview: {e}")
        return ""
    valid = set(tops)

    def check(m):  # keep only links to papers that are really in this week's list
        nums = [int(n) for n in re.findall(r"\d+", m.group(2)) if int(n) in valid]
        return f"[{m.group(1)}](#p{','.join(map(str, nums))})" if nums else m.group(1)
    return LINK.sub(check, out)


# ---------- publish ----------
FIELDS = ("id", "title", "authors", "venue", "url", "date", "score", "topics", "summary",
          "highlight", "suggested_by", "suggest_note")
SCREENED_FIELDS = ("title", "url", "venue", "score", "topics", "suggested_by")


def publish(cfg, today, since, papers, intro, fetched, screened):
    selected = {p["id"] for p in papers}
    week = {
        "week": today.isoformat(), "since": since.isoformat(), "until": today.isoformat(),
        "fetched": fetched, "overview": intro,
        "papers": [{k: p.get(k) for k in FIELDS if p.get(k) not in (None, "")} for p in papers],
        # everything that was looked at, for the "All screened papers" section
        "screened": [dict({k: p.get(k) for k in SCREENED_FIELDS if p.get(k) not in (None, "")},
                          selected=p["id"] in selected)
                     for p in sorted(screened, key=lambda p: -p.get("score", 0))],
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
        plain = LINK.sub(lambda m: m.group(1), data.get("overview") or "")
        body = f"<p>{escape(plain)}</p><ul>" + "".join(
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

    # lab members' suggestions are always included, even if seen in an earlier week
    done, failed = [], []
    by_id = {p["id"]: p for p in papers}
    for s in suggestions.open_suggestions():
        sp = suggestions.resolve(s, cfg["contact_email"])
        if sp is None:
            failed.append(s["number"])
            continue
        done.append(s["number"])
        if sp["id"] in by_id:
            by_id[sp["id"]].update(suggested_by=sp["suggested_by"], suggest_note=sp["suggest_note"])
        else:
            papers.append(sp)
            by_id[sp["id"]] = sp
    if args.dry_run:
        for p in papers[:15]:
            print(f"  - [{p['venue']}] {p['title'][:100]}")
        return

    import anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    scored = screen(client, cfg, papers) if papers else []
    for p in papers:                      # suggestions count even if screening failed
        if p.get("suggested_by") and "score" not in p:
            p.update(score=0, topics=[])
            scored.append(p)
    ranked = sorted((p for p in scored if p["score"] >= cfg["min_relevance"]
                     and not p.get("suggested_by")), key=lambda p: p["score"], reverse=True)
    for k, p in enumerate(ranked):
        p["highlight"] = k < cfg["top_picks"]
    suggested = [p for p in scored if p.get("suggested_by")]
    relevant = ranked[:max(cfg["max_papers"] - len(suggested), 0)]
    relevant = sorted(relevant + suggested, key=lambda p: (not p.get("highlight"), -p["score"]))
    if relevant:
        summarize(client, cfg, relevant)
    intro = overview(client, cfg, relevant) if relevant else ""

    publish(cfg, today, since, relevant, intro, len(papers), scored)
    for p in papers:
        seen[p["id"]] = today.isoformat()
    save_seen(seen, today)
    if done or failed:
        suggestions.save_pending(done, failed, today.isoformat())


if __name__ == "__main__":
    main()
