"""
ingest_papers.py — Pull and cache open-access quant-finance papers (arXiv q-fin).

Run on your machine (network required):  python backend/knowledge/ingest_papers.py
Caches metadata + abstracts to backend/knowledge/papers.json. arXiv's API is
public and explicitly intended for programmatic access (be polite: ≤1 req/3s).

SSRN/AQR/CBOE are intentionally not scraped here — check their terms and add an
adapter if your use is permitted; abstracts from arXiv cover the signal-design
needs of this system.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

OUT = Path(__file__).parent / "papers.json"
NS = {"a": "http://www.w3.org/2005/Atom"}

QUERIES = [
    "cat:q-fin.TR AND abs:options",          # trading & market microstructure
    "cat:q-fin.PR AND abs:volatility",       # pricing
    "cat:q-fin.PM AND abs:momentum",         # portfolio management
    "cat:q-fin.ST AND abs:machine learning", # statistical finance
]


def fetch(query: str, max_results: int = 25) -> list[dict]:
    url = ("http://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": query, "sortBy": "submittedDate", "sortOrder": "descending",
        "max_results": max_results}))
    with urllib.request.urlopen(url, timeout=30) as r:
        tree = ET.fromstring(r.read())
    out = []
    for e in tree.findall("a:entry", NS):
        out.append({
            "id": e.findtext("a:id", "", NS),
            "title": " ".join(e.findtext("a:title", "", NS).split()),
            "abstract": " ".join(e.findtext("a:summary", "", NS).split()),
            "published": e.findtext("a:published", "", NS),
            "authors": [a.findtext("a:name", "", NS) for a in e.findall("a:author", NS)],
            "query": query,
        })
    return out


def main():
    papers, seen = [], set()
    for q in QUERIES:
        print(f"arXiv: {q}")
        for p in fetch(q):
            if p["id"] not in seen:
                seen.add(p["id"])
                papers.append(p)
        time.sleep(3)
    OUT.write_text(json.dumps(papers, indent=1))
    print(f"Cached {len(papers)} papers → {OUT}")


if __name__ == "__main__":
    main()
