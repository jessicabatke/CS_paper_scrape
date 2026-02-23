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
# ACL Anthology XML data is hosted on GitHub — we fetch only the current year's files.
ACL_GITHUB_XML_BASE = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/"
ACL_GITHUB_XML_DIR = "https://api.github.com/repos/acl-org/acl-anthology/contents/data/xml"


def fetch_acl_xml_file_list():
    """Return list of XML filenames in the ACL Anthology GitHub data/xml directory."""
    resp = requests.get(ACL_GITHUB_XML_DIR, timeout=30)
    resp.raise_for_status()
    files = resp.json()
    return [f["name"] for f in files if f["name"].endswith(".xml")]


def parse_acl_xml(xml_content, current_year):
    """
    Parse an ACL Anthology XML file and return paper dicts for the current year.
    ACL XML structure: <collection id="..."><volume id="..."><paper id="...">
    """
    papers = []
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as e:
        log.warning(f"XML parse error: {e}")
        return papers

    collection_id = root.get("id", "")

    for volume in root.findall("volume"):
        volume_id = volume.get("id", "")

        # Get volume-level year from <meta>
        vol_year = ""
        vol_meta = volume.find("meta")
        if vol_meta is not None:
            year_el = vol_meta.find("year")
            if year_el is not None:
                vol_year = year_el.text or ""

        for paper in volume.findall("paper"):
            paper_id = paper.get("id", "")

            # Paper-level year overrides volume-level year
            year_el = paper.find("year")
            year = (year_el.text or vol_year) if year_el is not None else vol_year

            if year != current_year:
                continue

            title_el = paper.find("title")
            title = "".join(title_el.itertext()) if title_el is not None else ""

            abstract_el = paper.find("abstract")
            abstract = "".join(abstract_el.itertext()) if abstract_el is not None else ""

            authors = []
            for author in paper.findall("author"):
                first = author.findtext("first", "")
                last = author.findtext("last", "")
                authors.append(f"{first} {last}".strip())

            # e.g. 2025.acl-long.1
            acl_id = f"{collection_id}-{volume_id}.{paper_id}"
            url = urljoin(ACL_BASE, acl_id)

            month_el = paper.find("month")
            month = month_el.text if month_el is not None else ""
            published = f"{month} {year}".strip()

            papers.append({
                "title": title,
                "abstract": abstract,
                "url": url,
                "authors": ", ".join(authors),
                "published": published,
                "source": "ACL Anthology",
                "_acl_id": acl_id,
            })

    return papers


def fetch_acl_recent_papers(lookback_days):
    """
    Fetch recent ACL Anthology papers by pulling current-year XML files from GitHub.
    Returns a list of paper dicts.
    """
    current_year = str(datetime.datetime.utcnow().year)

    log.info("Fetching ACL Anthology XML file list from GitHub...")
    xml_files = fetch_acl_xml_file_list()

    # Only process files that start with the current year (e.g. 2025.acl-long.xml)
    current_year_files = [f for f in xml_files if f.startswith(current_year)]
    log.info(f"Found {len(current_year_files)} XML files for {current_year}")

    papers = []
    for filename in current_year_files:
        url = ACL_GITHUB_XML_BASE + filename
        log.info(f"Fetching {url}")
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            file_papers = parse_acl_xml(resp.content, current_year)
            papers.extend(file_papers)
            time.sleep(0.5)  # be polite to GitHub
        except Exception as e:
            log.warning(f"Failed to fetch/parse {filename}: {e}")

    log.info(f"Fetched {len(papers)} ACL papers from current year XML files")
    return papers


def fetch_acl_fulltext(acl_id):
    """
    Fetch the ACL Anthology paper page to extract affiliation info.
    """
    url = urljoin(ACL_BASE, acl_id)
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, "html.parser")
            text = soup.get_text(separator=" ")[:50000]
            return text
    except Exception as e:
        log.debug(f"Could not fetch ACL page for {acl_id}: {e}")
    return None


def scrape_acl(lookback_days, seen_urls):
    papers = fetch_acl_recent_papers(lookback_days)
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
            log.info(f"Fetching ACL fulltext for affiliation check: {p['_acl_id']}")
            fulltext = fetch_acl_fulltext(p["_acl_id"])
            if fulltext and passes_affiliation_filter(fulltext):
                results.append(p)
            time.sleep(1)

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
