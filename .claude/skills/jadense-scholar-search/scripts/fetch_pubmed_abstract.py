#!/usr/bin/env python3
"""Fetch PubMed's own abstract text for records that still lack one after enrichment.

When: a selected paper has no ``abstract`` after provider enrichment (some
      publishers deposit none in OpenAlex/Crossref, and some provider responses
      omit abstracts that do exist on PubMed).
Do:   ``uv run python scripts/fetch_pubmed_abstract.py <PMID> [<PMID> ...]``
      (run from the package directory; Python stdlib only). Each record prints
      a ``PMID <id> — <title>`` header followed by the abstract on the next
      line(s), with structured ``LABEL:`` prefixes kept and whitespace
      collapsed; records without an abstract print ``NO_ABSTRACT`` (when a
      reading channel truncates long lines, redirect stdout to a file and
      read from the file).
      Set ``NCBI_API_KEY`` / ``NCBI_TOOL`` to raise the E-utilities rate limit;
      an optional ``NCBI_EMAIL`` is forwarded when present.
Why:  The PubMed record is the authoritative fallback for a missing abstract,
      and silently translating a truncated or non-abstract field corrupts the
      deliverable. Recover the text first; mark the gap if no source has it.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def text_of(node: ET.Element | None) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def fetch(pmid: str, timeout: float = 30.0) -> ET.Element:
    params = {"db": "pubmed", "id": pmid, "retmode": "xml"}
    for env, key in (("NCBI_API_KEY", "api_key"), ("NCBI_TOOL", "tool"), ("NCBI_EMAIL", "email")):
        value = os.environ.get(env)
        if value:
            params[key] = value
    url = EFETCH + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return ET.fromstring(response.read())


def render(article: ET.Element) -> tuple[str, str]:
    title = text_of(article.find("./MedlineCitation/Article/ArticleTitle"))
    parts = []
    for node in article.findall("./MedlineCitation/Article/Abstract/AbstractText"):
        body = " ".join("".join(node.itertext()).split())
        if not body:
            continue
        label = (node.get("Label") or "").strip()
        parts.append(f"{label}: {body}" if label else body)
    return title, " ".join(parts)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not argv:
        print(__doc__.strip().splitlines()[0])
        print("usage: fetch_pubmed_abstract.py <PMID> [<PMID> ...]", file=sys.stderr)
        return 2
    status = 0
    blocks = []
    for pmid in argv:
        try:
            root = fetch(pmid)
        except Exception as exc:  # network / HTTP / XML errors are per-record
            print(f"PMID {pmid}: fetch failed: {exc}", file=sys.stderr)
            status = 1
            continue
        article = root.find(".//PubmedArticle")
        if article is None:
            print(f"PMID {pmid}: no PubmedArticle in response", file=sys.stderr)
            status = 1
            continue
        title, abstract = render(article)
        blocks.append(f"PMID {pmid} — {title}" if title else f"PMID {pmid}")
        blocks.append(abstract if abstract else "NO_ABSTRACT")
        blocks.append("")
    sys.stdout.write("\n".join(blocks).rstrip() + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
