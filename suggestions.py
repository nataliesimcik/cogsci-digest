"""Paper suggestions from lab members, made as GitHub issues.

The weekly run reads open issues labelled "paper-suggestion" (or titled
"Paper suggestion: ..."), finds the paper behind the link or DOI, and adds it to
the digest. After the site is published, `python suggestions.py close` comments
on each issue with a link to the digest and closes it.

Runs only inside GitHub Actions, where GITHUB_TOKEN and GITHUB_REPOSITORY exist.
"""
import json
import os
import re
import sys

import feedparser

import sources
from common import ROOT, load_config

LABEL = "paper-suggestion"
TITLE_PREFIX = "paper suggestion"
PENDING = ROOT / ".pending_suggestions.json"   # handed from the build step to the close step
API = "https://api.github.com"


def _gh(method, path, token, **kw):
    import requests
    r = requests.request(method, f"{API}{path}", timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, **kw)
    r.raise_for_status()
    return r.json() if r.text else {}


def _env():
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    return (token, repo) if token and repo else (None, None)


# ---------- reading issues ----------
def _field(body, heading):
    m = re.search(rf"###\s*{heading}[^\n]*\n+(.*?)(?=\n###|\Z)", body or "", re.S | re.I)
    value = m.group(1).strip() if m else ""
    return "" if value == "_No response_" else value


def open_suggestions():
    token, repo = _env()
    if not token:
        return []
    try:
        issues = _gh("GET", f"/repos/{repo}/issues?state=open&per_page=100", token)
    except Exception as e:
        print(f"  ! Couldn't read suggestions: {e}")
        return []
    out = []
    for it in issues:
        if "pull_request" in it:
            continue
        labels = {l["name"] for l in it.get("labels", [])}
        if LABEL not in labels and not it["title"].lower().startswith(TITLE_PREFIX):
            continue
        body = it.get("body") or ""
        first_line = body.strip().splitlines()[0] if body.strip() else ""
        link = _field(body, "Link or DOI") or first_line
        out.append({
            "number": it["number"], "user": it["user"]["login"],
            "title": re.sub(r"(?i)^paper suggestion:?\s*", "", it["title"]).strip(),
            "link": link.strip(), "why": _field(body, "Why"),
        })
    print(f"  Suggestions: {len(out)} open")
    return out


# ---------- finding the paper ----------
def resolve(s, email):
    """Return a paper dict for a suggestion, or None if the link can't be understood."""
    link = s["link"]
    doi = re.search(r"10\.\d{4,9}/[^\s\"<>]+", link)
    arx = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:v\d+)?(?:\.pdf)?(?:[?#].*)?$", link) \
        or re.fullmatch(r"(?:arxiv:)?(\d{4}\.\d{4,5})(?:v\d+)?", link.strip(), re.I)
    osf = re.search(r"osf\.io/(?:preprints/[a-z]+/)?([a-z0-9]{5})(?:[/?#]|$)", link)

    paper = None
    try:
        if arx:
            paper = _from_arxiv(arx.group(1))
        elif doi:
            paper = _from_crossref(doi.group(0).rstrip(".,;)"), email)
        elif osf:
            paper = _from_osf(osf.group(1))
    except Exception as e:
        print(f"  ! Suggestion #{s['number']}: {e}")

    if paper is None:
        if not link.startswith("http"):
            return None
        paper = {"id": f"url:{link}", "title": s["title"] or link, "abstract": "",
                 "authors": "", "url": link, "venue": "Suggested link", "date": ""}
    if not paper["title"]:
        paper["title"] = s["title"] or link
    paper.update(suggested_by=s["user"], suggest_note=s["why"], issue=s["number"])
    return paper


def _from_crossref(doi, email):
    r = sources._get(f"https://api.crossref.org/works/{doi}", params={"mailto": email})
    it = r.json()["message"]
    authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() for a in it.get("author", [])]
    venue = (it.get("container-title") or [""])[0] or it.get("publisher", "")
    if it.get("subtype") == "preprint" or it.get("type") == "posted-content":
        venue = venue or "Preprint"
    paper = {
        "id": f"doi:{doi.lower()}", "title": sources._clean((it.get("title") or [""])[0]),
        "abstract": sources._clean(it.get("abstract")), "authors": sources._short_authors(authors),
        "url": f"https://doi.org/{doi}", "venue": venue,
        "date": "-".join(str(x) for x in it.get("created", {}).get("date-parts", [[""]])[0]),
    }
    if not paper["abstract"]:
        sources.enrich_abstracts([paper], email)
    return paper


def _from_arxiv(arxiv_id):
    r = sources._get("http://export.arxiv.org/api/query", params={"id_list": arxiv_id})
    e = feedparser.parse(r.text).entries[0]
    return {"id": f"arxiv:{arxiv_id}", "title": sources._clean(e.title),
            "abstract": sources._clean(e.summary),
            "authors": sources._short_authors([a.name for a in e.get("authors", [])]),
            "url": f"https://arxiv.org/abs/{arxiv_id}", "venue": "arXiv",
            "date": e.get("published", "")[:10]}


def _from_osf(osf_id):
    a = sources._get(f"https://api.osf.io/v2/preprints/{osf_id}/").json()["data"]["attributes"]
    return {"id": f"psyarxiv:{osf_id}", "title": sources._clean(a.get("title")),
            "abstract": sources._clean(a.get("description")), "authors": "",
            "url": f"https://osf.io/{osf_id}", "venue": "PsyArXiv",
            "date": (a.get("date_created") or "")[:10]}


# ---------- after publishing ----------
def save_pending(done, failed, week):
    PENDING.write_text(json.dumps({"week": week, "done": done, "failed": failed}))


def close():
    token, repo = _env()
    if not token or not PENDING.exists():
        print("No suggestions to close.")
        return
    cfg = load_config()
    p = json.loads(PENDING.read_text())
    link = f"{cfg.get('site_url', '').rstrip('/')}/#week={p['week']}"
    messages = [(n, f"Thanks! This paper is in the digest for the week of {p['week']}: {link}")
                for n in p["done"]]
    messages += [(n, "I couldn't find a paper at this link. Please open a new suggestion "
                     "with a DOI (like 10.1016/...) or a direct link to the paper page.")
                 for n in p["failed"]]
    for number, text in messages:
        try:
            _gh("POST", f"/repos/{repo}/issues/{number}/comments", token, json={"body": text})
            _gh("PATCH", f"/repos/{repo}/issues/{number}", token,
                json={"state": "closed", "state_reason": "completed" if number in p["done"] else "not_planned"})
            print(f"  Closed suggestion #{number}")
        except Exception as e:
            print(f"  ! Couldn't close #{number}: {e}")
    PENDING.unlink()


if __name__ == "__main__":
    if sys.argv[1:] == ["close"]:
        close()
