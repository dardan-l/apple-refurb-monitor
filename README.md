# Apple Refurb Monitor

Polls Apple's refurbished Mac mini store every 15 minutes via GitHub Actions and pushes a phone notification through [ntfy.sh](https://ntfy.sh) when a listing matches your filter.

Built for monitoring **Mac mini M4 Pro 48GB** but the filter is configurable via env vars.

## How it works

- `refurb_monitor.py` fetches `https://www.apple.com/shop/refurbished/mac/mac-mini` and parses the **JSON-LD blocks** Apple embeds for SEO. Each block has the product title (`name`) plus full specs in the `description` field — RAM, storage, display, ports. Filtering happens against `name + description` combined.
- `state.json` tracks currently-listed matching SKUs. If a SKU delists and re-lists, you get a fresh notification.
- A SKU is only added to `seen_skus` if the ntfy POST succeeds. If ntfy is down for a cycle, the alert retries on the next run instead of being lost.
- Two "deadman" checks fire low-priority alerts if the parser looks broken. The **count deadman** trips when total listings drop below `MIN_LISTINGS` (default 50). The **health deadman** trips when the count is fine but fewer than 50% of listings contain `"ssd"` in their haystack — this catches subtle Apple JSON-LD shape changes that would silently kill matching against `description`-resident terms (RAM, storage). Both flags are sticky — one alert per outage, not one per cron tick.
- ntfy errors are scrubbed before logging (the topic name never reaches CI logs, even on 5xx).
- The Actions workflow commits `state.json` back to the repo when it changes.

Stack: Python 3 stdlib only, no pip dependencies.

## Setup

1. **Install ntfy on your phone.** Get the iOS or Android app and subscribe to a topic with a hard-to-guess name (anyone with the topic name can read your notifications). Example: `dardan-refurb-7f3a92`.

2. **Create a GitHub repo and push this project.** **Use a public repo if possible** — GitHub Actions is free and unlimited on public repos. On private repos, this workflow's 15-minute cron runs ~2,880 minutes/month, which exceeds the 2,000-minute free tier and costs ~$7/month or stops mid-month. The repo content (code + state.json) isn't sensitive; the ntfy topic stays in a secret. If you need it private, drop the cron to `*/30` to fit in free tier.

3. **Add the ntfy topic as a repo secret.**
   - Repo → Settings → Secrets and variables → Actions → New repository secret
   - Name: `NTFY_TOPIC`
   - Value: your topic name (e.g. `dardan-refurb-7f3a92`)

4. **(Optional) Customize via repo variables** without editing code:
   - `MATCH_ALL` — comma-separated terms, all must appear in title+description (default: `mac mini,m4 pro,48gb`)
   - `MATCH_ANY` — optional, at least one must appear (default: empty)
   - `MIN_LISTINGS` — deadman floor (default: 50)

5. **Trigger the first run manually** to verify: Actions tab → `refurb-monitor` workflow → Run workflow.

## Local testing

```bash
python3 refurb_monitor.py --status
```

Prints all currently listed Mac refurbs, the active filter, and which ones match. No notifications sent. With the default filter, expect zero matches today (Apple is sold out due to DRAM shortage and pre-M5 inventory clearing) but a non-zero total listings count proves the parser works.

To dry-run a real send locally:

```bash
NTFY_TOPIC=your-topic MATCH_ALL="mac mini" python3 refurb_monitor.py
```

## Caveats

- **GitHub cron lag.** Scheduled workflows can lag 10–30 minutes during high-load windows. Acceptable for refurbs since stock typically sits hours, not minutes.
- **ntfy is public-by-obscurity.** Free tier has no auth — anyone with your topic name can read it. Pick something non-guessable, don't post it anywhere. Successful POST also doesn't prove the push reached your phone, so test once at setup.
- **State commits accumulate.** Each state change is a commit. Squash periodically if it bothers you.
- **JSON-LD format may change.** If `--status` reports zero listings, Apple changed their schema. The deadman alert will tell you.
- **Retire when M5 Pro Mac mini ships** (expected post-WWDC, June 2026). Update filter terms or scrap the project.

## Files

- `refurb_monitor.py` — JSON-LD parser + matcher + notifier
- `.github/workflows/monitor.yml` — cron schedule, env wiring, state commit
- `state.json` — `{seen_skus, count_alerted, health_alerted}`
- `.gitignore` — keeps `__pycache__/` and friends out of commits
