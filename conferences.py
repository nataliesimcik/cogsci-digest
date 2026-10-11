"""Refresh conference dates and deadlines for the site's side panel.

Each conference in conferences.yaml is looked up with Claude + web search at most
every `refresh_days`. Results are cached in docs/data/conferences.json with the
source page, so every date on the site links to where it came from. Values set by
hand in conferences.yaml always win.

Usage:
  python conferences.py            # refresh stale entries
  python conferences.py --force    # refresh everything
"""
import argparse
import json
from datetime import date, timedelta

import yaml

from common import ROOT, DATA, load_config, parse_json, ask, write_json

OUT = DATA / "conferences.json"
REGIONS = {"Europe", "North America", "Online", "Other"}
MANUAL_FIELDS = ("location", "region", "dates", "start_date", "deadlines")

SYSTEM = """You find submission deadlines for academic conferences. Today is {today}.
Use web search to find the NEXT edition of the conference that has not yet taken place
(its main event starts after today). Prefer the official conference website.

Reply with ONLY a JSON object:
{{
  "edition": "name and year of the next edition",
  "location": "City, Country" or "Online",
  "region": "Europe" | "North America" | "Online" | "Other",
  "dates": "human-readable event dates, or \\"\\" if unknown",
  "start_date": "YYYY-MM-DD or \\"\\"",
  "deadlines": [{{"label": "e.g. Abstracts, Full papers, Early registration", "date": "YYYY-MM-DD"}}],
  "status": "announced" (deadlines published for this edition) | "not announced",
  "typical_timing": "if not announced: when the deadline usually falls, based on past editions, e.g. \\"usually early February\\"; otherwise \\"\\"",
  "source_url": "the page where you found the deadlines",
  "official_url": "the conference's official site"
}}
Only include dates you actually found on a web page for THIS edition. Never guess a
date: if deadlines aren't published, return an empty list and status "not announced".
Use "Online" as region only for fully virtual events."""


def lookup(client, cfg, conf, today):
    user = f"Conference: {conf['name']}"
    if conf.get("url"):
        user += f"\nKnown website: {conf['url']}"
    tools = [{"type": cfg["conferences"]["web_search_tool"], "name": "web_search", "max_uses": 5}]
    text = ask(client, cfg["models"]["write"], SYSTEM.format(today=today.isoformat()),
               user, max_tokens=2000, tools=tools)
    r = parse_json(text, "{")
    if r.get("region") not in REGIONS:
        r["region"] = "Other"
    r["deadlines"] = [d for d in r.get("deadlines", [])
                      if isinstance(d, dict) and len(str(d.get("date", ""))) == 10]
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    confs = yaml.safe_load((ROOT / "conferences.yaml").read_text()) or []
    today = date.today()
    stale_before = (today - timedelta(days=cfg["conferences"]["refresh_days"])).isoformat()
    try:
        cache = {c["name"]: c for c in json.loads(OUT.read_text()).get("conferences", [])}
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    client = None
    out = []
    for conf in confs:
        entry = dict(cache.get(conf["name"], {}), name=conf["name"])
        # a past event or a passed last deadline means it's time to look for the next edition
        passed = entry.get("start_date") and entry["start_date"] < today.isoformat()
        if args.force or passed or entry.get("checked", "") < stale_before:
            if client is None:
                import anthropic
                client = anthropic.Anthropic()
            try:
                found = lookup(client, cfg, conf, today)
                entry.update(found, checked=today.isoformat())
                print(f"  ✓ {conf['name']}: {found.get('edition', '?')}, "
                      f"{len(found['deadlines'])} deadline(s)")
            except Exception as e:
                print(f"  ! {conf['name']}: {e} (keeping cached data)")
        for k in MANUAL_FIELDS:            # hand-entered values win
            if k in conf:
                entry[k] = conf[k]
        if "deadlines" in conf:
            entry["status"] = "announced"
        entry.setdefault("official_url", conf.get("url", ""))
        out.append(entry)

    write_json(OUT, {"updated": today.isoformat(), "conferences": out})
    print(f"Wrote {len(out)} conferences")


if __name__ == "__main__":
    main()
