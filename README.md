# Static RSS Reader

A tiny RSS/Atom reader that runs in GitHub Actions and publishes a static site to GitHub Pages. No server, no database — just a list of feeds.

## Setup

1. Create a new GitHub repo and push these files to the `main` branch.
2. In the repo, go to **Settings → Pages** and set **Source** to **GitHub Actions**.
3. Go to **Actions → Build feed site → Run workflow** (or just push a commit).

Your reader will be live at `https://<user>.github.io/<repo>/` and rebuilds every hour.

## Adding feeds

Edit `feeds.txt` — one feed per line, with an optional display name:

```
https://example.com/feed.xml My Favourite Blog
https://another.site/atom.xml
```

Commit the change and the site rebuilds automatically.

## What you get

- `index.html` — all items from every feed, newest first, grouped by day
- `feeds/<name>.html` — one page per feed
- `atom.xml` — a merged Atom feed of everything (subscribe to it in another reader)
- Light/dark mode, mobile-friendly, no JavaScript
- Feeds that fail to load are listed at the bottom of the front page instead of breaking the build

## Configuration

Tweak the constants at the top of `build.py`:

| Setting | Default | Meaning |
|---|---|---|
| `SITE_TITLE` | `My Feed Reader` | Header and page title |
| `MAX_ITEMS` | 300 | Items shown on the front page |
| `MAX_PER_FEED` | 50 | Items kept from each feed |
| `SUMMARY_CHARS` | 280 | Length of item summaries |

Change the schedule by editing the `cron` line in `.github/workflows/build.yml`. Note that GitHub disables scheduled workflows after 60 days with no repo activity; re-enable from the Actions tab if that happens.

## Run locally

```
pip install -r requirements.txt
python build.py
open site/index.html
```

Feeds are fetched over `http(s)` only, with a timeout and a 5 MiB size limit. Connections (and redirects) to loopback, private, link-local and other non-public addresses are refused. To read a feed served from your own machine or LAN, run `RSS_ALLOW_PRIVATE=1 python build.py`.

Tests: `python -m unittest discover -s tests -v`


# Credits
Claude Opus 5.5

"can you make rss feed reader that generates a static site for the feed after running in a github action"
