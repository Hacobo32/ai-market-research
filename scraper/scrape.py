#!/usr/bin/env python3
"""
Market Research Scraper
========================
Pulls attention signals for a topic from 8 sources and writes the
combined result to data.json at the repo root, where the static
dashboard (index.html) reads it from.

Sources: Reddit, Hacker News, GitHub (repo search), YouTube,
Polymarket, arXiv, GitHub Trending, X/Twitter.

Run manually:   python scraper/scrape.py
Run in CI:      see .github/workflows/scrape.yml (runs this on a schedule)

Config: edit TOPIC and the per-source query strings below.
"""

import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Config — change these to retarget the whole dashboard at a new topic
# ---------------------------------------------------------------------------

TOPIC = "AI"
LOOKBACK_DAYS = 7
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data.json")

# arXiv categories to pull from — swap these if TOPIC changes domain
ARXIV_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL"]

# Negative keywords appended to the X/Twitter query to kill spam at the source
TWITTER_NEGATIVE_KEYWORDS = ["-course", "-giveaway", "-crypto"]

# X/Twitter is the one paid source (twitterapi.io). Reads from an env var
# so the key is never committed — set it as a GitHub Actions secret named
# TWITTERAPI_KEY. If it's not set, the script just skips this source.
TWITTERAPI_KEY = os.environ.get("TWITTERAPI_KEY")

HEADERS = {"User-Agent": "market-research-dashboard/1.0"}
CUTOFF = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. Reddit — RSS feed + Arctic Shift archive for scores/comments
# ---------------------------------------------------------------------------

def fetch_reddit(topic):
    log("Fetching Reddit (RSS + Arctic Shift)...")
    results = []
    try:
        resp = requests.get(
            "https://www.reddit.com/search.rss",
            params={"q": topic, "sort": "new"},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)

        post_ids = []
        parsed = []
        for entry in entries:
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            updated = entry.findtext("atom:updated", default="", namespaces=ns)

            # Reddit RSS strips most of the "submitted by /u/x [link] [comments]"
            # junk already for the title field, but clean up stragglers.
            title = re.sub(r"\s*submitted by.*$", "", title, flags=re.IGNORECASE).strip()

            # Extract the post ID out of a URL like:
            # https://www.reddit.com/r/xxx/comments/<id>/slug/
            m = re.search(r"/comments/([a-z0-9]+)/", link or "")
            post_id = m.group(1) if m else None
            if post_id:
                post_ids.append(post_id)

            parsed.append({
                "title": title,
                "url": link,
                "published": updated,
                "post_id": post_id,
                "score": None,
                "comments": None,
            })

        # Backfill scores/comment counts from Arctic Shift, a free public
        # Reddit archive, since RSS doesn't include them.
        # NOTE: verify this endpoint shape against Arctic Shift's current
        # docs (arctic-shift.photon-reddit.com) before relying on it —
        # third-party archive APIs change without notice.
        scores_by_id = {}
        if post_ids:
            try:
                ids_param = ",".join(f"t3_{pid}" for pid in post_ids)
                ar = requests.get(
                    "https://arctic-shift.photon-reddit.com/api/posts/ids",
                    params={"ids": ids_param},
                    headers=HEADERS,
                    timeout=15,
                )
                if ar.ok:
                    ar_data = ar.json()
                    for post in ar_data.get("data", []):
                        scores_by_id[post.get("id")] = {
                            "score": post.get("score"),
                            "comments": post.get("num_comments"),
                        }
            except requests.RequestException as e:
                log(f"  Arctic Shift lookup failed (non-fatal): {e}")

        for item in parsed:
            extra = scores_by_id.get(item["post_id"], {})
            item["score"] = extra.get("score")
            item["comments"] = extra.get("comments")
            del item["post_id"]
            results.append(item)

    except Exception as e:
        log(f"  Reddit fetch failed: {e}")

    return results


# ---------------------------------------------------------------------------
# 2. Hacker News — Algolia search API (free, no key)
# ---------------------------------------------------------------------------

def fetch_hackernews(topic):
    log("Fetching Hacker News (Algolia)...")
    results = []
    try:
        resp = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"query": topic, "tags": "story"},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        for hit in resp.json().get("hits", []):
            created = hit.get("created_at")
            try:
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                created_dt = None
            if created_dt and created_dt < CUTOFF:
                continue
            results.append({
                "title": hit.get("title"),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                "points": hit.get("points"),
                "comments": hit.get("num_comments"),
                "published": created,
            })
    except Exception as e:
        log(f"  Hacker News fetch failed: {e}")
    return results


# ---------------------------------------------------------------------------
# 3. GitHub — official Search API (repos pushed in the last week)
# ---------------------------------------------------------------------------

def fetch_github_repos(topic):
    log("Fetching GitHub repos (Search API)...")
    results = []
    try:
        since = CUTOFF.strftime("%Y-%m-%d")
        query = f"{topic} pushed:>{since}"
        headers = dict(HEADERS)
        gh_token = os.environ.get("GITHUB_TOKEN")  # optional, just raises rate limit
        if gh_token:
            headers["Authorization"] = f"Bearer {gh_token}"
        resp = requests.get(
            "https://api.github.com/search/repositories",
            params={"q": query, "sort": "stars", "order": "desc", "per_page": 20},
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        for repo in resp.json().get("items", []):
            results.append({
                "name": repo.get("full_name"),
                "url": repo.get("html_url"),
                "description": repo.get("description"),
                "stars": repo.get("stargazers_count"),
                "language": repo.get("language"),
                "pushed_at": repo.get("pushed_at"),
            })
    except Exception as e:
        log(f"  GitHub repo search failed: {e}")
    return results


# ---------------------------------------------------------------------------
# 4. YouTube — yt-dlp search (android client, to dodge the "web" player bug)
# ---------------------------------------------------------------------------

def fetch_youtube(topic):
    log("Fetching YouTube (yt-dlp)...")
    results = []
    queries = [topic, f"{topic} news"]
    seen_ids = set()

    for q in queries:
        search_spec = f"ytsearchdate10:{q}"
        try:
            proc = subprocess.run(
                [
                    "yt-dlp",
                    "--dump-json",
                    "--extractor-args", "youtube:player_client=android",
                    "--flat-playlist",
                    search_spec,
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if proc.returncode != 0:
                log(f"  yt-dlp error for query '{q}': {proc.stderr[:300]}")
                continue
            for line in proc.stdout.splitlines():
                if not line.strip():
                    continue
                try:
                    video = json.loads(line)
                except json.JSONDecodeError:
                    continue
                vid = video.get("id")
                if not vid or vid in seen_ids:
                    continue

                upload_date = video.get("upload_date")  # YYYYMMDD
                if upload_date:
                    try:
                        uploaded_dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
                        if uploaded_dt < CUTOFF:
                            continue
                    except ValueError:
                        pass

                seen_ids.add(vid)
                results.append({
                    "title": video.get("title"),
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "channel": video.get("channel") or video.get("uploader"),
                    "upload_date": upload_date,
                    "view_count": video.get("view_count"),
                })
        except FileNotFoundError:
            log("  yt-dlp is not installed — skipping YouTube. (pip install yt-dlp)")
            break
        except subprocess.TimeoutExpired:
            log(f"  yt-dlp timed out for query '{q}'")
        except Exception as e:
            log(f"  YouTube fetch failed for query '{q}': {e}")

    return results


# ---------------------------------------------------------------------------
# 5. Polymarket — public search endpoint + relevance filter
# ---------------------------------------------------------------------------

def fetch_polymarket(topic):
    log("Fetching Polymarket...")
    results = []
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/public-search",
            params={"q": topic},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response shape has varied historically; handle a couple of forms.
        events = data.get("events") if isinstance(data, dict) else data
        events = events or []

        topic_lower = topic.lower()
        for event in events:
            title = (event.get("title") or event.get("question") or "")
            if topic_lower not in title.lower():
                continue  # relevance filter — Polymarket search returns a lot of noise
            markets = event.get("markets") or [event]
            for market in markets:
                price = market.get("outcomePrices") or market.get("lastTradePrice")
                results.append({
                    "title": title,
                    "url": f"https://polymarket.com/event/{event.get('slug', '')}",
                    "price": price,
                    "volume": market.get("volume") or event.get("volume"),
                })
    except Exception as e:
        log(f"  Polymarket fetch failed: {e}")
    return results


# ---------------------------------------------------------------------------
# 6. arXiv — official academic API
# ---------------------------------------------------------------------------

def fetch_arxiv(categories):
    log("Fetching arXiv...")
    results = []
    try:
        cat_query = "+OR+".join(f"cat:{c}" for c in categories)
        resp = requests.get(
            "http://export.arxiv.org/api/query"
            f"?search_query={cat_query}&sortBy=submittedDate&sortOrder=descending&max_results=30",
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.content)
        for entry in root.findall("atom:entry", ns):
            published = entry.findtext("atom:published", default="", namespaces=ns)
            try:
                pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                pub_dt = None
            if pub_dt and pub_dt < CUTOFF:
                continue
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            link = entry.findtext("atom:id", default="", namespaces=ns)
            authors = [
                a.findtext("atom:name", default="", namespaces=ns)
                for a in entry.findall("atom:author", ns)
            ]
            results.append({
                "title": re.sub(r"\s+", " ", title),
                "url": link,
                "summary": re.sub(r"\s+", " ", summary),
                "authors": authors,
                "published": published,
            })
    except Exception as e:
        log(f"  arXiv fetch failed: {e}")
    return results


# ---------------------------------------------------------------------------
# 7. GitHub Trending — HTML scrape (no official API for this one)
# ---------------------------------------------------------------------------

def fetch_github_trending():
    log("Fetching GitHub Trending (HTML scrape)...")
    results = []
    try:
        resp = requests.get(
            "https://github.com/trending", params={"since": "weekly"}, headers=HEADERS, timeout=15
        )
        resp.raise_for_status()
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            log("  beautifulsoup4 not installed — falling back to Search API")
            return fetch_github_repos_fallback()

        soup = BeautifulSoup(resp.text, "html.parser")
        for article in soup.select("article.Box-row"):
            name_el = article.select_one("h2 a")
            if not name_el:
                continue
            name = name_el.get_text(strip=True).replace(" ", "").replace("/", "/")
            href = name_el.get("href", "")
            desc_el = article.select_one("p")
            lang_el = article.select_one('[itemprop="programmingLanguage"]')
            stars_el = article.select_one('a[href$="/stargazers"]')
            results.append({
                "name": name,
                "url": f"https://github.com{href}",
                "description": desc_el.get_text(strip=True) if desc_el else None,
                "language": lang_el.get_text(strip=True) if lang_el else None,
                "stars_this_week": stars_el.get_text(strip=True) if stars_el else None,
            })
    except Exception as e:
        log(f"  GitHub Trending scrape failed, falling back to Search API: {e}")
        return fetch_github_repos_fallback()
    return results


def fetch_github_repos_fallback():
    # No official trending API exists, so if the HTML scrape breaks
    # (layout change, block, etc.) fall back to a plain star-sorted search.
    return fetch_github_repos(TOPIC)


# ---------------------------------------------------------------------------
# 8. X / Twitter — twitterapi.io (the one paid source)
# ---------------------------------------------------------------------------

def fetch_twitter(topic):
    if not TWITTERAPI_KEY:
        log("Skipping X/Twitter — no TWITTERAPI_KEY set.")
        return []

    log("Fetching X/Twitter (twitterapi.io)...")
    results = []
    query_base = f"{topic} " + " ".join(TWITTER_NEGATIVE_KEYWORDS)

    # min_faves thresholds are erratic on this API — try a ladder and keep
    # the first batch that comes back with results.
    for min_faves in (500, 300, 100, 20):
        try:
            resp = requests.get(
                "https://api.twitterapi.io/twitter/tweet/advanced_search",
                params={
                    "query": f"{query_base} min_faves:{min_faves}",
                    "queryType": "Latest",
                },
                headers={**HEADERS, "X-API-Key": TWITTERAPI_KEY},
                timeout=20,
            )
            if not resp.ok:
                continue
            tweets = resp.json().get("tweets", [])
            if tweets:
                for t in tweets:
                    results.append({
                        "text": t.get("text"),
                        "url": t.get("url") or t.get("twitterUrl"),
                        "author": (t.get("author") or {}).get("userName"),
                        "likes": t.get("likeCount"),
                        "created_at": t.get("createdAt"),
                    })
                break
        except Exception as e:
            log(f"  X/Twitter fetch failed at min_faves={min_faves}: {e}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log(f"Starting market research scrape for topic: {TOPIC!r}")
    data = {
        "topic": TOPIC,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "sources": {},
    }

    fetchers = [
        ("reddit", lambda: fetch_reddit(TOPIC)),
        ("hackernews", lambda: fetch_hackernews(TOPIC)),
        ("github_repos", lambda: fetch_github_repos(TOPIC)),
        ("youtube", lambda: fetch_youtube(TOPIC)),
        ("polymarket", lambda: fetch_polymarket(TOPIC)),
        ("arxiv", lambda: fetch_arxiv(ARXIV_CATEGORIES)),
        ("github_trending", fetch_github_trending),
        ("twitter", lambda: fetch_twitter(TOPIC)),
    ]

    for key, fn in fetchers:
        try:
            data["sources"][key] = fn()
            log(f"  -> {key}: {len(data['sources'][key])} items")
        except Exception as e:
            log(f"  -> {key} failed entirely: {e}")
            data["sources"][key] = []
        time.sleep(1)  # be a little polite between sources

    out_path = os.path.abspath(OUTPUT_PATH)
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    log(f"Wrote {out_path}")


if __name__ == "__main__":
    sys.exit(main())
