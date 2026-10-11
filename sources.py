"""Fetchers for new papers and preprints. Each returns a list of dicts:
{id, title, abstract, authors, url, venue, date}
"""
import re
import time
from datetime import date

import feedparser
import requests

UA = "cogsci-weekly-digest/1.0"
TIMEOUT = 30


def _clean(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)          # strip JATS/HTML tags
    return re.sub(r"\s+", " ", text).strip()


def _short_authors(names, limit=3):
    names = [n for n in names if n]
    if not names:
        return ""
    return ", ".join(names[:limit]) + (" et al." if len(names) > limit else "")


def _get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA}, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"{r.status_code}")
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


# ---------- Journals via Crossref ----------
def fetch_crossref(journals, since: date, email):
    out = []
    for j in journals:
        try:
            r = _get(
                f"https://api.crossref.org/journals/{j['issn']}/works",
                params={
                    "filter": f"from-created-date:{since.isoformat()},type:journal-article",
                    "rows": 200,
                    "select": "DOI,title,abstract,author,URL,created",
                    "mailto": email,
                },
            )
        except Exception as e:
            print(f"  ! Crossref {j['name']}: {e}")
            continue
        items = r.json().get("message", {}).get("items", [])
        for it in items:
            title = _clean((it.get("title") or [""])[0])
            if not title or title.lower() in {"editorial board", "issue information", "contents"}:
                continue
            authors = [f"{a.get('given', '')} {a.get('family', '')}".strip()
                       for a in it.get("author", [])]
            out.append({
                "id": f"doi:{it['DOI'].lower()}",
                "title": title,
                "abstract": _clean(it.get("abstract")),
                "authors": _short_authors(authors),
                "url": f"https://doi.org/{it['DOI']}",
                "venue": j["name"],
                "date": "-".join(str(p) for p in it["created"]["date-parts"][0]),
            })
        print(f"  Crossref {j['name']}: {len(items)}")
        time.sleep(1)
    return out


# ---------- arXiv ----------
def fetch_arxiv(queries, since: date):
    out = []
    for q in queries:
        try:
            r = _get("http://export.arxiv.org/api/query", params={
                "search_query": q, "sortBy": "submittedDate",
                "sortOrder": "descending", "max_results": 200,
            })
        except Exception as e:
            print(f"  ! arXiv '{q}': {e}")
            continue
        feed = feedparser.parse(r.text)
        n = 0
        for e in feed.entries:
            published = date(*e.published_parsed[:3])
            if published < since:
                continue
            arxiv_id = e.id.rsplit("/abs/", 1)[-1].split("v")[0]
            out.append({
                "id": f"arxiv:{arxiv_id}",
                "title": _clean(e.title),
                "abstract": _clean(e.summary),
                "authors": _short_authors([a.name for a in e.get("authors", [])]),
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "venue": "arXiv",
                "date": published.isoformat(),
            })
            n += 1
        print(f"  arXiv '{q}': {n}")
        time.sleep(3)  # arXiv asks for a pause between calls
    return out


# ---------- PsyArXiv (OSF API) ----------
def fetch_psyarxiv(since: date, max_pages=10):
    out = []
    url = "https://api.osf.io/v2/preprints/"
    params = {
        "filter[provider]": "psyarxiv",
        "filter[date_created][gte]": since.isoformat(),
        "page[size]": 100,
    }
    for _ in range(max_pages):
        try:
            data = _get(url, params=params).json()
        except Exception as e:
            print(f"  ! PsyArXiv: {e}")
            break
        for p in data.get("data", []):
            a = p.get("attributes", {})
            out.append({
                "id": f"psyarxiv:{p['id']}",
                "title": _clean(a.get("title")),
                "abstract": _clean(a.get("description")),
                "authors": "",
                "url": p.get("links", {}).get("html") or f"https://osf.io/{p['id']}",
                "venue": "PsyArXiv",
                "date": (a.get("date_created") or "")[:10],
            })
        url, params = data.get("links", {}).get("next"), None
        if not url:
            break
    print(f"  PsyArXiv: {len(out)}")
    return out


# ---------- bioRxiv ----------
def fetch_biorxiv(categories, since: date, until: date, max_calls=40):
    wanted = {c.lower() for c in categories}
    seen, out, cursor = set(), [], 0
    for _ in range(max_calls):
        try:
            data = _get(f"https://api.biorxiv.org/details/biorxiv/"
                        f"{since.isoformat()}/{until.isoformat()}/{cursor}").json()
        except Exception as e:
            print(f"  ! bioRxiv: {e}")
            break
        coll = data.get("collection", [])
        for p in coll:
            if p.get("category", "").lower() not in wanted:
                continue
            if str(p.get("version")) != "1" or p["doi"] in seen:  # new preprints only
                continue
            seen.add(p["doi"])
            out.append({
                "id": f"doi:{p['doi'].lower()}",
                "title": _clean(p.get("title")),
                "abstract": _clean(p.get("abstract")),
                "authors": _short_authors([s.strip() for s in p.get("authors", "").split(";")]),
                "url": f"https://www.biorxiv.org/content/{p['doi']}v1",
                "venue": f"bioRxiv ({p['category']})",
                "date": p.get("date", ""),
            })
        if len(coll) < 100:
            break
        cursor += len(coll)
    print(f"  bioRxiv: {len(out)}")
    return out


# ---------- Abstract enrichment (OpenAlex, then PubMed) ----------
def _from_inverted(inv):
    if not inv:
        return ""
    pos = sorted((i, w) for w, idxs in inv.items() for i in idxs)
    return " ".join(w for _, w in pos)


def enrich_abstracts(papers, email):
    """Fill in missing abstracts for DOI-based papers. Elsevier and some other
    publishers don't deposit abstracts with Crossref."""
    missing = {p["id"][4:]: p for p in papers if p["id"].startswith("doi:") and not p["abstract"]}
    if not missing:
        return
    before = len(missing)

    dois = list(missing)
    for i in range(0, len(dois), 40):
        chunk = dois[i:i + 40]
        try:
            data = _get("https://api.openalex.org/works", params={
                "filter": "doi:" + "|".join(chunk), "per-page": 50,
                "select": "doi,abstract_inverted_index", "mailto": email,
            }).json()
        except Exception as e:
            print(f"  ! OpenAlex: {e}")
            continue
        for w in data.get("results", []):
            doi = (w.get("doi") or "").lower().replace("https://doi.org/", "")
            text = _from_inverted(w.get("abstract_inverted_index"))
            if doi in missing and text:
                missing.pop(doi)["abstract"] = text

    dois = list(missing)
    eutils = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    for i in range(0, len(dois), 20):
        chunk = dois[i:i + 20]
        try:
            ids = _get(f"{eutils}/esearch.fcgi", params={
                "db": "pubmed", "retmode": "json", "retmax": 50,
                "term": " OR ".join(f'"{d}"[doi]' for d in chunk),
            }).json()["esearchresult"]["idlist"]
            time.sleep(0.4)
            if not ids:
                continue
            xml = _get(f"{eutils}/efetch.fcgi", params={
                "db": "pubmed", "retmode": "xml", "id": ",".join(ids)}).text
            time.sleep(0.4)
        except Exception as e:
            print(f"  ! PubMed: {e}")
            continue
        import xml.etree.ElementTree as ET
        for art in ET.fromstring(xml).iter("PubmedArticle"):
            doi = next((a.text.lower() for a in art.iter("ArticleId")
                        if a.get("IdType") == "doi" and a.text), None)
            text = " ".join("".join(t.itertext()) for t in art.iter("AbstractText"))
            if doi in missing and text.strip():
                missing.pop(doi)["abstract"] = _clean(text)

    print(f"  Abstracts filled: {before - len(missing)}/{before} (rest are title-only)")
