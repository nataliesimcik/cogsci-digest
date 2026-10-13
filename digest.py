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
        papers += sources.fetch_psyarxiv(since, cfg["contact_email"])
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
SUMMARY_SYSTEM = """You tell members of a cognitive science lab about new papers, the way a
well-read colleague would over coffee. For each paper write 2-3 natural sentences in plain
language: what the researchers did, what they found, and why it's interesting.

Style: warm and clear, not academic boilerplate. Vary how sentences start; don't open
with "This paper", "This study" or "The authors" every time. Explain jargon briefly if
it's essential, and skip it if it isn't. No hype words (groundbreaking, novel, sheds light).

Accuracy: use only what the title and abstract say. Never invent findings, numbers or
methods. If no abstract is given, return an empty string as the summary.
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


LINK = re.compile(r"\[([^\]]+)\]\(\s*#?p?([\d,\s]+)\)")


def overview(client, cfg, papers):
    """Overview text. Links to papers are stored as [phrase](#p3,7), where the numbers
    are positions in the published paper list, so the site can scroll to them."""
    tops = [i for i, p in enumerate(papers) if p.get("highlight") or p.get("suggested_by")] \
        or list(range(min(8, len(papers))))
    text = "\n\n".join(f"[{i}] {papers[i]['title']}: "
                       f"{papers[i].get('summary') or (papers[i].get('abstract') or '')[:900]}" for i in tops)
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
FIELDS = ("id", "title", "authors", "venue", "kind", "url", "date", "score", "topics", "summary",
          "highlight", "suggested_by", "suggest_note")
SCREENED_FIELDS = ("title", "url", "venue", "kind", "score", "topics", "suggested_by")


def publish(cfg, today, since, papers, intro, fetched, screened):
    selected = {p["id"] for p in papers}
    week = {
        "week": today.isoformat(), "since": since.isoformat(), "until": today.isoformat(),
        "fetched": fetched, "overview": intro,
        # an empty abstract means "not available yet", so that key is always kept
        "papers": [dict({k: p.get(k) for k in FIELDS if p.get(k) not in (None, "")},
                        abstract=p.get("abstract") or "") for p in papers],
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
        "display": cfg.get("display", "both"),
        "sources": site_sources(cfg),
        "weeks": weeks,
    })
    write_feed(cfg, weeks)
    print(f"Published week {today.isoformat()}: {len(papers)} papers")


def site_sources(cfg):
    """What's screened every week, for the Sources section at the bottom of the site."""
    s = cfg["sources"]
    cats = []
    for q in s.get("arxiv_queries", []):
        cats += [c for c in re.findall(r"cat:([\w.\-]+)", q) if c not in cats]
    preprints = []
    names = {"q-bio.NC": "Neurons and Cognition", "cs.CL": "Computation and Language",
             "cs.AI": "Artificial Intelligence", "cs.LG": "Machine Learning", "q-bio.QM": "Quantitative Methods",
             "cs.HC": "Human-Computer Interaction", "stat.ML": "Machine Learning (stats)"}
    if cats:
        preprints.append({"name": "arXiv", "detail": ", ".join(f"{names[c]} ({c})" if c in names else c for c in cats)})
    if s.get("psyarxiv"):
        preprints.append({"name": "PsyArXiv", "detail": ""})
    if s.get("biorxiv_categories"):
        preprints.append({"name": "bioRxiv", "detail": ", ".join(s["biorxiv_categories"])})
    return {"journals": sorted(j["name"] for j in s.get("journals", [])), "preprints": preprints}


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


def backfill_abstracts(cfg, weeks=4):
    """Papers whose abstract wasn't available yet get another try in later weeks."""
    try:
        recent = json.loads((DATA / "index.json").read_text())["weeks"][:weeks]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return
    for w in recent:
        path = DATA / "weeks" / f"{w}.json"
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        changed = False
        for p in data["papers"]:              # PsyArXiv authors, which older runs didn't have
            if p["id"].startswith("psyarxiv:") and not p.get("authors"):
                rec = sources.psyarxiv_record(p["id"][9:], cfg["contact_email"])
                if rec and rec["authors"]:
                    p["authors"] = rec["authors"]
                    p["abstract"] = p.get("abstract") or rec["abstract"]
                    changed = True
        todo = [p for p in data["papers"] if not p.get("abstract")]
        if not todo:
            if changed:
                write_json(path, data)
            continue
        print(f"  Retrying {len(todo)} missing abstract(s) from week {w}")
        for p in todo:
            p["abstract"] = ""
        sources.enrich_abstracts([p for p in todo if p["id"].startswith("doi:")], cfg["contact_email"])
        for p in todo:                       # preprints: ask arXiv / PsyArXiv directly
            try:
                if p["id"].startswith("arxiv:"):
                    p["abstract"] = suggestions._from_arxiv(p["id"][6:])["abstract"]
                elif p["id"].startswith("psyarxiv:"):
                    p["abstract"] = suggestions._from_osf(p["id"][9:])["abstract"]
            except Exception as e:
                print(f"  ! {p['id']}: {e}")
        write_json(path, data)


def fill_summaries(client, cfg, weeks=4, redo=False, skip=None):
    """Summaries for papers whose abstract arrived later. With redo, rewrite them all."""
    if cfg.get("display", "both") == "abstract":
        return
    try:
        recent = json.loads((DATA / "index.json").read_text())["weeks"][:weeks]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return
    for w in recent:
        if w == skip:
            continue
        path = DATA / "weeks" / f"{w}.json"
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        todo = [p for p in data["papers"] if p.get("abstract") and (redo or not p.get("summary"))]
        if redo:   # drop old summaries written without an abstract
            for p in data["papers"]:
                if not p.get("abstract"):
                    p.pop("summary", None)
        if todo:
            print(f"  Writing {len(todo)} summary(ies) for week {w}")
            summarize(client, cfg, todo)
        if todo or redo:
            write_json(path, data)


def relink(cfg):
    import anthropic
    index = json.loads((DATA / "index.json").read_text())
    week = index["weeks"][0]
    path = DATA / "weeks" / f"{week}.json"
    backfill_abstracts(cfg, weeks=1)          # cheap: open databases only, no model calls
    data = json.loads(path.read_text())
    if not data["papers"]:
        print("Latest week has no papers; nothing to do.")
        return
    client = anthropic.Anthropic()
    fill_summaries(client, cfg, weeks=1, redo=True)   # summaries in the current style, from real abstracts
    data = json.loads(path.read_text())
    data["overview"] = overview(client, cfg, data["papers"])
    write_json(path, data)
    index["display"] = cfg.get("display", "both")
    index["sources"] = site_sources(cfg)
    write_json(DATA / "index.json", index)
    write_feed(cfg, index["weeks"])
    print(f"Rebuilt overview for week {week} ({len(LINK.findall(data['overview']))} links)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int)
    ap.add_argument("--relink", action="store_true",
                    help="only rebuild the latest week's overview (with paper links); no fetching")
    ap.add_argument("--force", action="store_true",
                    help="rebuild today's digest even if it already exists (costs a full run)")
    args = ap.parse_args()

    cfg = load_config()
    if args.relink:
        return relink(cfg)
    today = date.today()
    since = today - timedelta(days=args.days or cfg.get("lookback_days", 7))

    # a second full run on the same day would only repeat the work (and the cost)
    if not args.dry_run and not args.force and (DATA / "weeks" / f"{today.isoformat()}.json").exists():
        print(f"Today's digest ({today.isoformat()}) already exists, so nothing was done and nothing was spent. "
              "To refresh only the overview, run the workflow with 'Only rebuild this week's overview' ticked.")
        return

    try:
        backfill_abstracts(cfg)
    except Exception as e:          # never let the retry stop the weekly digest
        print(f"  ! Abstract retry skipped: {e}")
    seen = load_seen()
    # papers first seen today stay "new", so a same-day re-run rebuilds this week instead of emptying it
    papers = [p for p in collect(cfg, since, today) if seen.get(p["id"], today.isoformat()) >= today.isoformat()]
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
    if relevant and cfg.get("display", "both") != "abstract":
        summarize(client, cfg, relevant)
        fill_summaries(client, cfg, skip=today.isoformat())   # earlier weeks' late-arriving abstracts
    intro = overview(client, cfg, relevant) if relevant else ""

    publish(cfg, today, since, relevant, intro, len(papers), scored)
    for p in papers:
        seen[p["id"]] = today.isoformat()
    save_seen(seen, today)
    if done or failed:
        suggestions.save_pending(done, failed, today.isoformat())


if __name__ == "__main__":
    main()
