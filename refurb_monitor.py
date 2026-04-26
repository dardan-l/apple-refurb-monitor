#!/usr/bin/env python3
"""Poll Apple's refurb Mac mini store and push ntfy notifications for matches."""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REFURB_URL = "https://www.apple.com/shop/refurbished/mac/mac-mini"
STATE_FILE = Path(__file__).resolve().parent / "state.json"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15"
)
# Tolerate any attribute order on the script tag (e.g. id= or data-* before type=).
JSON_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# A near-universal token in Mac refurb descriptions. If it disappears from
# most listings while the count stays healthy, Apple's JSON-LD shape changed
# and filter terms targeting the description (e.g. "48gb") silently miss.
HEALTH_TOKEN = "ssd"
HEALTH_THRESHOLD = 0.5


def fetch_page(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_listings(html):
    """Return refurb product listings from JSON-LD blocks as list of dicts."""
    items = {}
    for m in JSON_LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for d in candidates:
            if not isinstance(d, dict):
                continue
            t = d.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" not in types:
                continue
            name = d.get("name", "")
            if "Refurbished" not in name:
                continue
            offers = d.get("offers")
            sku = ""
            price = None
            if isinstance(offers, list) and offers:
                first = offers[0] or {}
                sku = first.get("sku", "")
                price = first.get("price")
            elif isinstance(offers, dict):
                sku = offers.get("sku", "")
                price = offers.get("price")
            if not sku:
                continue
            url = d.get("url", "")
            description = d.get("description", "")
            items[sku] = {
                "sku": sku,
                "title": name,
                "url": url,
                "description": description,
                "price": price,
                "haystack": (name + " " + description).lower(),
            }
    return sorted(items.values(), key=lambda i: i["sku"])


def matches(item, match_all, match_any):
    h = item["haystack"]
    if not all(term in h for term in match_all):
        return False
    if match_any and not any(term in h for term in match_any):
        return False
    return True


def parse_terms(s):
    return [x.strip().lower() for x in s.split(",") if x.strip()] if s else []


def health_ratio(listings):
    if not listings:
        return 0.0
    return sum(1 for it in listings if HEALTH_TOKEN in it["haystack"]) / len(listings)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"seen_skus": [], "count_alerted": False, "health_alerted": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def notify(topic, *, title, body, click_url=None, priority="high", tags="moneybag,apple"):
    """POST to ntfy.sh. Raises sanitized RuntimeError on failure (never the URL)."""
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except urllib.error.HTTPError as e:
        # Suppress chain so the original exception (which stringifies the URL
        # including the topic name) doesn't reach CI logs.
        raise RuntimeError(f"ntfy HTTP {e.code}") from None
    except Exception as e:
        raise RuntimeError(f"ntfy {type(e).__name__}") from None


def main():
    status_only = "--status" in sys.argv
    match_all = parse_terms(os.environ.get("MATCH_ALL", "mac mini,m4 pro,48gb"))
    match_any = parse_terms(os.environ.get("MATCH_ANY", ""))
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    try:
        min_listings = int(os.environ.get("MIN_LISTINGS", "50"))
    except ValueError:
        min_listings = 50

    html = fetch_page(REFURB_URL)
    listings = parse_listings(html)
    matched = [i for i in listings if matches(i, match_all, match_any)]
    health = health_ratio(listings)

    if status_only:
        print(f"Total refurb listings: {len(listings)}")
        print(f"Filter MATCH_ALL: {match_all}")
        print(f"Filter MATCH_ANY: {match_any or '(none)'}")
        print(f"Count floor (MIN_LISTINGS): {min_listings}")
        print(f"Health ({HEALTH_TOKEN!r} substring): {health:.0%}")
        print(f"Matches: {len(matched)}")
        matched_skus = {i["sku"] for i in matched}
        for it in listings:
            mark = "*" if it["sku"] in matched_skus else " "
            print(f"  [{mark}] {it['sku']}: {it['title']}")
        return 0

    state = load_state()
    count_alerted = bool(state.get("count_alerted", state.get("deadman_alerted", False)))
    health_alerted = bool(state.get("health_alerted", False))

    # Count deadman: listings collapsed below floor → don't trust the fetch.
    if len(listings) < min_listings:
        if not count_alerted and topic:
            try:
                notify(
                    topic,
                    title="Refurb monitor: low listings",
                    body=(
                        f"Only {len(listings)} listings found (floor {min_listings}). "
                        "Apple may have changed their HTML or blocked the scraper."
                    ),
                    priority="default",
                    tags="warning",
                )
                count_alerted = True
                print(f"Count deadman alert sent: {len(listings)} < {min_listings}")
            except Exception as e:
                print(f"Count deadman notify failed: {e}", file=sys.stderr)
        elif not topic:
            print(
                f"WARNING: count deadman tripped ({len(listings)} < {min_listings}) "
                "but NTFY_TOPIC unset",
                file=sys.stderr,
            )
        else:
            print(f"Count deadman still active ({len(listings)} < {min_listings}); not re-alerting")

        new_state = {
            "seen_skus": sorted(state.get("seen_skus", [])),
            "count_alerted": count_alerted,
            "health_alerted": health_alerted,
        }
        if new_state != state:
            save_state(new_state)
            print("State updated (count deadman flag)")
        else:
            print("No state change")
        return 0

    if count_alerted:
        print("Listings recovered above floor; clearing count deadman flag")
    count_alerted = False

    # Health deadman: count is fine but descriptions look stripped → filter
    # terms targeting description (like "48gb") will silently miss matches.
    if health < HEALTH_THRESHOLD:
        if not health_alerted and topic:
            try:
                notify(
                    topic,
                    title="Refurb monitor: degraded data",
                    body=(
                        f"Only {health:.0%} of {len(listings)} listings contain "
                        f"{HEALTH_TOKEN!r}. Apple's JSON-LD shape may have changed; "
                        "filter terms targeting RAM/storage may silently miss matches."
                    ),
                    priority="default",
                    tags="warning",
                )
                health_alerted = True
                print(f"Health deadman alert sent: only {health:.0%} have {HEALTH_TOKEN!r}")
            except Exception as e:
                print(f"Health deadman notify failed: {e}", file=sys.stderr)
        elif not topic:
            print(
                f"WARNING: health deadman tripped ({health:.0%} have {HEALTH_TOKEN!r}) "
                "but NTFY_TOPIC unset",
                file=sys.stderr,
            )
        else:
            print(f"Health deadman still active ({health:.0%} have {HEALTH_TOKEN!r}); not re-alerting")
    else:
        if health_alerted:
            print(f"Health recovered ({health:.0%} have {HEALTH_TOKEN!r}); clearing flag")
        health_alerted = False

    state_seen = set(state.get("seen_skus", []))
    matched_skus = {i["sku"] for i in matched}
    # Drop SKUs no longer listed so re-listings re-fire.
    new_seen = state_seen & matched_skus

    for item in matched:
        if item["sku"] in state_seen:
            continue
        if not topic:
            print(
                f"WARNING: NTFY_TOPIC not set; would notify {item['sku']}",
                file=sys.stderr,
            )
            continue
        try:
            notify(
                topic,
                title="Apple refurb match",
                body=f"{item['title']}\n{item['url']}",
                click_url=item["url"],
            )
            # Only mark seen on success — failures retry next run.
            new_seen.add(item["sku"])
            print(f"Notified: {item['sku']} {item['title']}")
        except Exception as e:
            print(f"Notify failed for {item['sku']}: {e}", file=sys.stderr)

    new_state = {
        "seen_skus": sorted(new_seen),
        "count_alerted": count_alerted,
        "health_alerted": health_alerted,
    }
    if new_state != state:
        save_state(new_state)
        print(f"State updated: tracking {len(new_state['seen_skus'])} matching SKU(s)")
    else:
        print(f"No state change ({len(matched)} matching, {len(listings)} total listings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
