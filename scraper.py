"""
Paper scraper for arXiv and ACL Anthology.
Filters papers by topic, abstract keywords, and author affiliation.
Outputs an RSS feed for use with GitHub Pages.

--- CONFIGURATION ---
Edit the sections below to tune your filters. No need to touch the logic beneath.
"""

import os
import re
import time
import json
import hashlib
import logging
import requests
import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
from bs4 import BeautifulSoup
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION — edit freely
# ============================================================

# How many days back to look for new papers
LOOKBACK_DAYS = 3

# Path where the RSS feed will be written
RSS_OUTPUT_PATH = "rss.xml"

# --- arXiv ---

ARXIV_CATEGORIES = [
    "cs.CL",  # Computation and Language
    "cs.AI",  # Artificial Intelligence
    "cs.LG",  # Machine Learning
    "cs.CR",  # Cryptography and Security (catches some safety work)
    "cs.CY",  # Computers and Society
]

# --- Shared keyword filters (used for both arXiv and ACL) ---

# A paper must contain at least one of these in its abstract to pass (case-insensitive substring)
LLM_TERMS = [
    "llm",
    "llms",
    "large language model",
    "large language models",
    "foundation model",
    "foundation models",
    "language model",
    "language models",
]

# The abstract keyword cluster — paper passes if abstract contains ANY of these (case-insensitive substring)
ABSTRACT_KEYWORDS = [
    "align",    # catches alignment, aligned, misaligned, etc.
    "values",
    "lingual",  # catches multilingual, crosslingual, etc.
    "safe",     # catches safety, unsafe, etc.
]

# The affiliation cluster — paper passes if fulltext contains ANY of these (case-insensitive substring)
# Checked for any paper that contains an LLM term but does NOT match the abstract keywords above
AFFILIATION_STRINGS = [
    "Chinese Academy of Sciences",
    "Beijing Key Laboratory of Safe AI and Superalignment",
    "Shanghai Artificial Intelligence Laboratory",
    "Beijing Institute of AI Safety and Governance",
]

# ============================================================
# FILTER LOGIC — edit only if you need to change the logic
# ============================================================

def contains_any(text, terms):
    """Case-insensitive substring match for any term in a list."""
    text_lower = text.lower()
    return any(t.lower() in text_lower for t in terms)


def passes_abstract_filter(abstract):
    """
    Returns True if:
      - abstract contains at least one LLM term, AND
      - abstract contains at least one keyword from ABSTRACT_KEYWORDS
    """
    if not abstract:
        return False
    return contains_any(abstract, LLM_TERMS) and contains_any(abstract, ABSTRACT_KEYWORDS)


def passes_affiliation_filter(fulltext):
    """Returns True if fulltext contains any of the target affiliation strings."""
    if not fulltext:
        return False
    return contains_any(fulltext, AFFILIATION_STRINGS)


def passes_filter(abstract, fulltext=None):
    """
    Full filter logic:
      topic_is_relevant (handled upstream by category/venue filtering)
      AND llm_term in abstract
      AND (abstract_keyword_match OR affiliation_match)
    """
    if not abstract:
        return False
    has_llm = contains_any(abstract, LLM_TERMS)
    if not has_llm:
        return False
    has_keyword = contains_any(abstract, ABSTRACT_KEYWORDS)
    has_affiliation = passes_affiliation_filter(fulltext) if fulltext else False
    return has_keyword or has_affiliation

# ============================================================
# RSS HELPERS
# ============================================================

def load_existing_rss(path):
    """Load existing RSS and return a set of already-seen paper URLs."""
    seen = set()
    if not os.path.exists(path):
        return seen
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        channel = root.find("channel")
        if channel is not None:
            for item in channel.findall("item"):
                link = item.findtext("link")
                if link:
                    seen.add(link.strip())
    except Exception as e:
        log.warning(f"Could not parse existing RSS: {e}")
    return seen


def build_rss(items, existing_path):
    """
    Build an RSS XML string from a list of paper dicts.
    Merges with existing items in the feed (new items on top).
    """
    existing_items = []
    if os.path.exists(existing_path):
        try:
            tree = ET.parse(existing_path)
            root = tree.getroot()
            channel = root.find("channel")
            if channel is not None:
                existing_items = channel.findall("item")
        except Exception as e:
            log.warning(f"Could not read existing RSS items: {e}")

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "AI Safety & Alignment Papers"
    ET.SubElement(channel, "link").text = "https://github.com"
    ET.SubElement(channel, "description").text = (
        "Daily feed of arXiv and ACL Anthology papers on LLM alignment, safety, and related topics."
    )
    ET.SubElement(channel, "lastBuildDate").text = datetime.datetime.utcnow().strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    # New items first
    for paper in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = paper["title"]
        ET.SubElement(item, "link").text = paper["url"]
        ET.SubElement(item, "description").text = paper.get("abstract", "")
        ET.SubElement(item, "author").text = paper.get("authors", "")
        ET.SubElement(item, "pubDate").text = paper.get("published", "")
        ET.SubElement(item, "source").text = paper.get("source", "")
        guid = ET.SubElement(item, "guid", isPermaLink="true")
        guid.text = paper["url"]

    # Append old items
    for old_item in existing_items:
        channel.append(old_item)

    xml_str = ET.tostring(rss, encoding="unicode")
    pretty = minidom.parseString(xml_str).toprettyxml(indent="  ")
    # Remove the extra XML declaration minidom adds
    lines = pretty.split("\n")
    if lines[0].startswith("<?xml"):
        pretty = "\n".join(lines[1:])
    return pretty

# ============================================================
# ARXIV SCRAPER
# ============================================================

ARXIV_API = "http://export.arxiv.org/api/query"
ARXIV_HTML_BASE = "https://arxiv.org/html/"

def fetch_arxiv_papers(lookback_days):
    """
    Query arXiv API for recent papers in the configured categories.
    Returns a list of dicts with title, abstract, url, authors, published.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=lookback_days)
    # Build category query
    cat_query = " OR ".join(f"cat:{c}" for c in ARXIV_CATEGORIES)
    query = f"({cat_query})"

    papers = []
    start = 0
    batch = 100

    while True:
        params = {
            "search_query": query,
            "start": start,
            "max_results": batch,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        log.info(f"Fetching arXiv batch start={start}")
        resp = requests.get(ARXIV_API, params=params, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.content, "xml")
        entries = soup.find_all("entry")
        if not entries:
            break

        stop = False
        for entry in entries:
            published_str = entry.find("published").get_text(strip=True)
            published_dt = datetime.datetime.fromisoformat(published_str.replace("Z", "+00:00")).replace(tzinfo=None)

            if published_dt < cutoff:
                stop = True
                break

            paper_id = entry.find("id").get_text(strip=True)
            # Canonical URL
            url = paper_id.replace("http://", "https://")
            title = entry.find("title").get_text(strip=True).replace("\n", " ")
            abstract = entry.find("summary").get_text(strip=True).replace("\n", " ")
            authors = ", ".join(a.find("name").get_text(strip=True) for a in entry.find_all("author"))

            papers.append({
                "title": title,
                "abstract": abstract,
                "url": url,
                "authors": authors,
                "published": published_str,
                "source": "arXiv",
                "_arxiv_id": paper_id.split("/abs/")[-1],
            })

        if stop or len(entries) < batch:
            break
        start += batch
        time.sleep(3)  # be polite to arXiv API

    log.info(f"Fetched {len(papers)} arXiv papers before filtering")
    return papers


def fetch_arxiv_fulltext(arxiv_id):
    """
    Attempt to fetch the HTML version of an arXiv paper for affiliation checking.
    Returns text or None.
    """
    url = f"{ARXIV_HTML_BASE}{arxiv_id}"
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            # Affiliations are usually near the top; grab first ~50KB of text for efficiency
            text = soup.get_text(separator=" ")[:50000]
            return text
    except Exception as e:
        log.debug(f"Could not fetch arXiv HTML for {arxiv_id}: {e}")
    return None


def scrape_arxiv(lookback_days, seen_urls):
    papers = fetch_arxiv_papers(lookback_days)
    results = []

    for p in papers:
        if p["url"] in seen_urls:
            continue

        abstract = p["abstract"]

        # Hard requirement: must contain an LLM-related term
        has_llm = contains_any(abstract, LLM_TERMS)
        if not has_llm:
            continue

        has_keyword = contains_any(abstract, ABSTRACT_KEYWORDS)

        if has_keyword:
            # Passes on abstract keywords alone — no need to fetch fulltext
            results.append(p)
        else:
            # Did not pass on abstract keywords — fetch fulltext and check affiliations
            log.info(f"Fetching fulltext for affiliation check: {p['_arxiv_id']}")
            fulltext = fetch_arxiv_fulltext(p["_arxiv_id"])
            if fulltext and passes_affiliation_filter(fulltext):
                results.append(p)
            time.sleep(1)

    log.info(f"arXiv: {len(results)} papers passed filter")
    return results

# ============================================================
# ACL ANTHOLOGY SCRAPER
# ============================================================

ACL_BASE = "https://aclanthology.org/"
ACL_RSS_URL = "https://aclanthology.org/papers/index.xml"

# Venue blocklist — papers whose proceedings title contains any of these
# substrings (case-insensitive) will be skipped before fetching their pages.
# Covers speech/audio, narrow MT, biomedical, historical, legal, OCR venues.
ACL_VENUE_BLOCKLIST = [
    # Speech & audio
    "Spoken Dialogue",
    "Speech and Language Processing",
    "Acoustics, Speech",
    "Spoken Language",
    "Speech Communication",
    "Text-to-Speech",
    "INTERSPEECH",
    "ICASSP",
    "Odyssey",
    "SLTU",
    # Machine translation (narrow)
    "Conference on Machine Translation",
    "Workshop on Machine Translation",
    "MT Summit",
    "Asia-Pacific Association for Machine Translation",
    "European Association for Machine Translation",
    "Workshop on Asian Translation",
    # Biomedical / clinical
    "BioNLP",
    "Clinical NLP",
    "ClinicalNLP",
    "Biomedical",
    "LOUHI",
    "BioCreative",
    "Health Informatics",
    "Medical NLP",
    # Historical, literary, cultural heritage
    "LaTeCH",
    "HistoInformatics",
    "Digital Humanities",
    "Cultural Heritage",
    "Literary",
    # Low-resource / morphology / linguistics
    "AmericasNLP",
    "SIGMORPHON",
    "Endangered Languages",
    "ComputEL",
    "Low-Resource",
    # Legal (narrow)
    "Legal and Law",
    "Natural Legal Language",
    "Workshop on Legal",
    # Document processing / OCR
    "Document Analysis",
    "Document Engineering",
    "ICDAR",
]


def is_blocked_venue(description):
    """Return True if the paper's venue matches the blocklist."""
    desc_lower = description.lower()
    return any(term.lower() in desc_lower for term in ACL_VENUE_BLOCKLIST)


def fetch_acl_page(url):
    """
    Fetch an ACL Anthology paper page and extract the abstract and full text.
    Returns (abstract, fulltext) tuple; either may be None on failure.

    ACL paper pages have the abstract in a <div class="acl-abstract"> or
    <span class="abstract-text"> element. We grab a broad text slice for
    affiliation checking.
    """
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            log.debug(f"ACL page returned {resp.status_code}: {url}")
            return None, None
        soup = BeautifulSoup(resp.content, "html.parser")

        # Try several known selectors for the abstract
        abstract = None
        for selector in [
            "span.abstract-text",
            "div.acl-abstract",
            "section#abstract",
            "div#abstract",
        ]:
            el = soup.select_one(selector)
            if el:
                abstract = el.get_text(separator=" ", strip=True)
                break

        # Fall back: look for a <strong>Abstract</strong> followed by text
        if not abstract:
            for tag in soup.find_all(["p", "div"]):
                text = tag.get_text(separator=" ", strip=True)
                if text.lower().startswith("abstract"):
                    abstract = text[len("abstract"):].strip().lstrip(":").strip()
                    if len(abstract) > 50:
                        break

        # Fulltext for affiliation checking — first 60KB of page text is enough
        fulltext = soup.get_text(separator=" ")[:60000]

        return abstract, fulltext

    except Exception as e:
        log.debug(f"Could not fetch ACL page {url}: {e}")
        return None, None


def fetch_acl_recent_papers(lookback_days):
    """
    Fetch the ACL Anthology RSS feed, filter to the lookback window,
    exclude blocked venues, then fetch each surviving paper's page
    to extract the abstract and run keyword/affiliation checks.

    RSS <item> structure:
        <title>Paper Title</title>
        <link>https://aclanthology.org/2026.acl-long.1/</link>
        <pubDate>Wed, 18 Feb 2026 00:00:00 +0000</pubDate>
        <guid>2026.acl-long.1</guid>
        <description>Author1, Author2 in Proceedings of ...</description>
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=lookback_days)

    log.info(f"Fetching ACL Anthology RSS feed...")
    resp = requests.get(ACL_RSS_URL, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "xml")
    items = soup.find_all("item")
    log.info(f"RSS feed contains {len(items)} items total")

    # --- Step 1: filter by date and venue blocklist ---
    candidates = []
    skipped_date = 0
    skipped_venue = 0

    for item in items:
        pub_str = item.findtext("pubDate", "").strip()
        published_dt = None
        if pub_str:
            try:
                published_dt = datetime.datetime.strptime(
                    pub_str, "%a, %d %b %Y %H:%M:%S %z"
                ).replace(tzinfo=None)
            except ValueError:
                pass

        # RSS is newest-first; stop once past the cutoff
        if published_dt and published_dt < cutoff:
            skipped_date += 1
            break

        description = item.findtext("description", "").strip()
        if is_blocked_venue(description):
            skipped_venue += 1
            continue

        title = item.findtext("title", "").strip()
        url = item.findtext("link", "").strip()
        acl_id = item.findtext("guid", "").strip()

        # Parse authors from description: "Author1, Author2 in Proceedings of ..."
        authors = ""
        if " in " in description:
            authors = description.split(" in ")[0].strip()

        candidates.append({
            "title": title,
            "url": url,
            "authors": authors,
            "published": pub_str,
            "source": "ACL Anthology",
            "_acl_id": acl_id,
            "_description": description,
        })

    log.info(
        f"After date filter: {skipped_date} too old, "
        f"{skipped_venue} blocked by venue, "
        f"{len(candidates)} candidates remaining"
    )

    # --- Step 2: fetch each candidate's page for abstract + affiliation ---
    papers = []
    for i, p in enumerate(candidates):
        log.info(f"Fetching ACL page {i+1}/{len(candidates)}: {p['url']}")
        abstract, fulltext = fetch_acl_page(p["url"])
        p["abstract"] = abstract or ""
        p["_fulltext"] = fulltext or ""
        papers.append(p)
        time.sleep(1)  # be polite to ACL servers

    log.info(f"Fetched pages for {len(papers)} ACL candidates")
    return papers


def scrape_acl(lookback_days, seen_urls):
    # fetch_acl_recent_papers already handles date filtering, venue blocklist,
    # and page fetching -- each paper dict has 'abstract' and '_fulltext' ready.
    papers = fetch_acl_recent_papers(lookback_days)
    results = []

    for p in papers:
        if p["url"] in seen_urls:
            continue

        abstract = p["abstract"]
        fulltext = p.get("_fulltext", "")

        # Hard requirement: must contain an LLM-related term in abstract
        if not contains_any(abstract, LLM_TERMS):
            continue

        # Pass if abstract keywords match OR affiliation match in fulltext
        has_keyword = contains_any(abstract, ABSTRACT_KEYWORDS)
        has_affiliation = passes_affiliation_filter(fulltext)

        if has_keyword or has_affiliation:
            results.append(p)

    log.info(f"ACL Anthology: {len(results)} papers passed filter")
    return results

# ============================================================
# MAIN
# ============================================================

def main():
    seen_urls = load_existing_rss(RSS_OUTPUT_PATH)
    log.info(f"Loaded {len(seen_urls)} already-seen URLs from existing RSS")

    all_papers = []

    # --- arXiv ---
    try:
        arxiv_papers = scrape_arxiv(LOOKBACK_DAYS, seen_urls)
        all_papers.extend(arxiv_papers)
    except Exception as e:
        log.error(f"arXiv scraping failed: {e}")

    # --- ACL Anthology ---
    try:
        acl_papers = scrape_acl(LOOKBACK_DAYS, seen_urls)
        all_papers.extend(acl_papers)
    except Exception as e:
        log.error(f"ACL Anthology scraping failed: {e}")

    log.info(f"Total new papers to add: {len(all_papers)}")

    rss_content = build_rss(all_papers, RSS_OUTPUT_PATH)
    with open(RSS_OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(rss_content)
    log.info(f"RSS feed written to {RSS_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
