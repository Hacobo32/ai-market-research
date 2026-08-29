# AI Market Research — Signal Board

A free, serverless market research dashboard. GitHub Actions scrapes 8
sources on a schedule and commits the results to `data.json`; GitHub
Pages serves `index.html`, which reads that file. Nothing runs 24/7,
so there's nothing to pay for.

Sources: Reddit, Hacker News, GitHub Search, GitHub Trending, YouTube,
Polymarket, arXiv, X/Twitter (the one paid source, ~pennies/run via
twitterapi.io — everything else is free).

## Setup

1. **Push this folder to a new public GitHub repo.**
   ```
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

2. **(Optional) Add your X/Twitter API key as a secret.**
   Only needed if you want the Twitter source populated.
   Repo → Settings → Secrets and variables → Actions → New repository secret
   → name it `TWITTERAPI_KEY`, value = your twitterapi.io key.
   Skip this and the scraper just returns an empty list for that source.

3. **Turn on GitHub Pages.**
   Repo → Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, folder `/ (root)`.
   You'll get a URL like `https://<you>.github.io/<repo>/`.

4. **Run the scrape once manually.**
   Repo → Actions tab → "Scrape market research data" → Run workflow.
   Watch it run, then check that `data.json` in the repo has real content
   (not the empty placeholder it ships with).

5. **Visit your Pages URL.** The dashboard should populate.

After that, it runs itself — every 6 hours by default (edit the `cron:`
line in `.github/workflows/scrape.yml` to change the schedule).

## Changing the topic

Edit the top of `scraper/scrape.py`:

- `TOPIC` — the search term used across Reddit, HN, GitHub, YouTube, Polymarket
- `ARXIV_CATEGORIES` — arXiv's own category codes (the AI ones are
  `cs.AI`, `cs.LG`, `cs.CL` — swap for whatever fits your new topic)
- `TWITTER_NEGATIVE_KEYWORDS` — spam terms to exclude from X results

## Local testing

```
pip install -r requirements.txt
python scraper/scrape.py
python -m http.server 8000
```
Then open `http://localhost:8000`. (Opening `index.html` directly from
disk won't work — browsers block `fetch()` on `file://` URLs.)

## Notes / things worth double-checking

- **Arctic Shift's endpoint** (used to backfill Reddit scores) is an
  unofficial third-party archive with no stable public docs — the
  request shape in `fetch_reddit()` is a best guess based on its
  general API pattern. If it stops returning scores, check
  arctic-shift.photon-reddit.com for its current endpoints and adjust.
- **GitHub Trending has no official API**, so `fetch_github_trending()`
  scrapes the HTML page directly. If GitHub changes that page's layout,
  this will silently fall back to a plain GitHub Search API call instead
  (see `fetch_github_repos_fallback()`).
- **twitterapi.io's `min_faves` filter is erratic** per the original
  build notes — the code tries a ladder of thresholds (500 → 300 → 100
  → 20) and keeps the first one that returns results.
- yt-dlp needs the `android` player client forced, since YouTube's
  default web client currently breaks yt-dlp searches.
