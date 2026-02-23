# paper-scraper

Daily arXiv + ACL Anthology scraper that filters for LLM alignment/safety papers and serves results as an RSS feed via GitHub Pages.

## Filter logic

A paper is included if:

```
(arXiv: cs.CL / cs.AI / cs.LG / cs.CR / cs.CY)  OR  (ACL Anthology: any venue)
AND (abstract contains an LLM-related term)
AND (
    (abstract contains: "align" OR "values" OR "lingual" OR "safe")
    OR
    (fulltext contains a target affiliation string)
)
```

All keyword matching is case-insensitive substring matching.

## Setup

### 1. Create the repo

Create a new GitHub repository and push these files to it.

### 2. Enable GitHub Pages

Go to **Settings → Pages** and set the source to **Deploy from a branch**, branch `main`, folder `/ (root)`. After the first scrape run, your RSS feed will be available at:

```
[https://<your-username>.github.io/<repo-name>/rss.xml](https://raw.githubusercontent.com/jessicabatke/CS_paper_scrape/main/rss.xml)
```

### 3. Run manually to test

Go to **Actions → Daily Paper Scrape → Run workflow** to trigger it manually before waiting for the daily schedule.

### 4. Subscribe to the feed

Add the GitHub Pages URL above to any RSS reader (e.g. NetNewsWire, Feedly, Inoreader).

## Tuning the filters

Everything you'd want to tweak is in the `CONFIGURATION` block at the top of `scraper.py`:

| Variable | What it controls |
|---|---|
| `LOOKBACK_DAYS` | How far back to look for papers |
| `ARXIV_CATEGORIES` | Which arXiv subject categories to include |
| `LLM_TERMS` | Terms that qualify a paper as LLM-related (abstract, any match) |
| `ABSTRACT_KEYWORDS` | Keyword cluster — paper passes if abstract contains any of these |
| `AFFILIATION_STRINGS` | Affiliation cluster — checked in fulltext if abstract keyword check fails |

To add or remove terms, just edit the relevant list. No other code needs to change.

## Schedule

The scraper runs daily at 07:00 UTC by default. To change this, edit the `cron` expression in `.github/workflows/scrape.yml`.

## Failure recovery

The scraper always looks back `LOOKBACK_DAYS` days (default: 3) and deduplicates against existing RSS entries, so if the action fails one day it will catch up on the next successful run without producing duplicates.
