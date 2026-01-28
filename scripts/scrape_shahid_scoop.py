#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scoop With Raya (Shahid) → RSS
- Crawls the public show + season pages
- Collects real episode URLs in the /en/player/episodes/... pattern
- Visits each episode page and scrapes title + description
- Writes two feeds:
    * scoop_with_raya_backfill_100.xml  (latest ~100 items)
    * scoop_with_raya_live.xml          (only newly discovered items)
- Keeps state in data/seen_ids.json so old episodes don't re-fire
"""

import os, re, time, json, html, hashlib, sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ------------------ CONFIG ------------------
SHOW_URL = "https://shahid.mbc.net/en/shows/scoop-with-raya/show-48981"
USER_AGENT = "Mozilla/5.0 (compatible; ScoopRayaFeedBot/1.0; +https://github.com/SusananinAtlanta/scoop-raya-feed)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
TIMEOUT = 20
SLEEP = 1.2  # politeness delay between requests (seconds)

# Output files (at repo root)
BACKFILL_XML = "scoop_with_raya_backfill_100.xml"
LIVE_XML     = "scoop_with_raya_live.xml"
STATE_PATH   = "data/seen_ids.json"

# Self links (RAW URLs Newsdesk can fetch)
SELF_RAW_BACKFILL = "https://raw.githubusercontent.com/SusananinAtlanta/scoop-raya-feed/refs/heads/main/scoop_with_raya_backfill_100.xml"
SELF_RAW_LIVE     = "https://raw.githubusercontent.com/SusananinAtlanta/scoop-raya-feed/refs/heads/main/scoop_with_raya_live.xml"

# Feed metadata
FEED_TITLE = "Scoop With Raya — Episodes (Shahid)"
FEED_LINK  = SHOW_URL
FEED_DESC  = "Auto-generated Scoop With Raya episode URLs from Shahid with scraped descriptions for Newsdesk matching."

# Discovery: only real episode URLs (avoid generic pages)
EP_URL_RE = re.compile(
    r"https://shahid\.mbc\.net/en/player/episodes/Scoop-With-Raya-season-\d{4}-episode-\d+/id-[0-9A-Za-z]+",
    re.IGNORECASE
)

# Backfill cap and live emission cap
BACKFILL_CAP = 100
LIVE_MAX_NEW = 3  # if many new items appear at once, emit at most 3 (set to 1 if you want strictly one)
# --------------------------------------------


def get(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def soupify(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, "lxml")


def rfc822(dt: datetime) -> str:
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def now_rfc822() -> str:
    return rfc822(datetime.now(timezone.utc))


def normalize_date(maybe_iso: str) -> str:
    # Convert ISO-ish → RFC-822 if possible; else return as-is
    if not maybe_iso:
        return now_rfc822()
    try:
        s = maybe_iso.replace("Z", "+00:00")
        if "T" not in s:
            s += "T00:00:00+00:00"
        dt = datetime.fromisoformat(s)
        return rfc822(dt)
    except Exception:
        return maybe_iso


def extract_episode_meta(url: str, html_text: str) -> dict:
    s = soupify(html_text)

    # Canonical
    canonical = None
    link_tag = s.select_one('link[rel="canonical"]')
    if link_tag and link_tag.get("href"):
        canonical = link_tag["href"].strip()
    if not canonical:
        canonical = url  # fallback; still unique and stable

    # Title/description
    def meta_content(selector):
        el = s.select_one(selector)
        return el.get("content").strip() if el and el.get("content") else None

    title = meta_content('meta[property="og:title"]')
    if not title and s.title and s.title.string:
        title = s.title.string.strip()
    if not title:
        title = "Scoop With Raya (Episode)"

    desc = meta_content('meta[property="og:description"]') or meta_content('meta[name="description"]')
    if not desc:
        desc = "No public synopsis was found on the page; open the link for full details."

    # JSON-LD date (uploadDate / datePublished / dateCreated)
    pub_date = None
    for tag in s.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue

        def scan(obj):
            nonlocal pub_date
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, (dict, list)):
                        scan(v)
                    elif isinstance(v, str) and k in ("uploadDate","datePublished","dateCreated") and len(v) >= 8:
                        pub_date = v
            elif isinstance(obj, list):
                for x in obj:
                    scan(x)

        scan(data)
        if pub_date:
            break

    pub_date = normalize_date(pub_date)

    # content:encoded — give Newsdesk real text to match
    blocks = []
    blocks.append(f"<p><strong>{html.escape(title)}</strong></p>")
    if desc:
        blocks.append(f"<p>{html.escape(desc)}</p>")
    blocks.append(f"<p>Source: {html.escape(canonical)}</p>")
    content_encoded = "\n".join(blocks)

    # Stable GUID (hash of canonical URL)
    guid = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    return {
        "title": title,
        "link": canonical,
        "guid": guid,
        "pubDate": pub_date,
        "description": desc,
        "content_encoded": content_encoded,
    }


def get_seed_html() -> list[str]:
    pages = []
    try:
        pages.append(get(SHOW_URL))
        time.sleep(SLEEP)
    except Exception as e:
        print(f("[WARN] Could not fetch show page: {e}"), file=sys.stderr)

    # Discover linked season pages to widen crawl
    season_links = set()
    for txt in pages:
        for m in re.finditer(r'href="([^"]*season-\d+[^"]*)"', txt):
            href = m.group(1)
            full = href if href.startswith("http") else urljoin(SHOW_URL, href)
            if "shahid.mbc.net" in full:
                season_links.add(full)

    for link in sorted(season_links):
        try:
            pages.append(get(link))
            time.sleep(SLEEP)
        except Exception as e:
            print(f"[WARN] Could not fetch season page {link}: {e}", file=sys.stderr)

    return pages


def discover_episode_urls(seed_html_list: list[str]) -> list[str]:
    urls = set()
    for txt in seed_html_list:
        for m in EP_URL_RE.finditer(txt):
            urls.add(m.group(0))
    return sorted(urls)


def write_rss(filename: str, items: list[dict], self_url: str):
    head = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "<channel>\n"
        f"<title>{html.escape(FEED_TITLE)}</title>\n"
        f"<link>{html.escape(FEED_LINK)}</link>\n"
        f"<description>{html.escape(FEED_DESC)}</description>\n"
        f"<language>en-us</language>\n"
        f"<lastBuildDate>{now_rfc822()}</lastBuildDate>\n"
        f'{html.escape(self_url)}\n'
        "<ttl>180</ttl>\n"
    )
    tail = "</channel>\n</rss>\n"

    blocks = []
    for it in items:
        blocks.append(
            "<item>\n"
            f"<title>{html.escape(it['title'])}</title>\n"
            f"<link>{html.escape(it['link'])}</link>\n"
            f"<guid isPermaLink=\"false\">{it['guid']}</guid>\n"
            f"<pubDate>{html.escape(it['pubDate'])}</pubDate>\n"
            f"<description>{html.escape(it['description'])}</description>\n"
            f"<content:encoded><![CDATA[{it['content_encoded']}]]></content:encoded>\n"
            "</item>\n"
        )

    with open(filename, "w", encoding="utf-8", newline="\n") as f:
        f.write(head + "".join(blocks) + tail)

    print(f"[OK] Wrote {filename} ({len(items)} items)")


def load_seen() -> set[str]:
    if not os.path.exists(STATE_PATH):
        return set()
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_guids", []))
    except Exception:
        return set()


def save_seen(seen: set[str]):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"seen_guids": sorted(seen)}, f, ensure_ascii=False, indent=2)


def main():
    # 1) Seeds: show + discovered season pages
    seeds = get_seed_html()

    # 2) Discover real episode URLs
    ep_urls = discover_episode_urls(seeds)
    if not ep_urls:
        print("[ERROR] No episode URLs found; site structure may have changed.", file=sys.stderr)
        sys.exit(2)
    print(f"[INFO] Found {len(ep_urls)} candidate episode URLs")

    # 3) Visit each episode page and extract metadata
    items = []
    for u in ep_urls:
        try:
            html_text = get(u)
            meta = extract_episode_meta(u, html_text)
            items.append(meta)
            time.sleep(SLEEP)
        except Exception as e:
            print(f"[WARN] Skipping {u}: {e}", file=sys.stderr)

    # 4) Sort newest first by pubDate (best effort)
    def keydate(it):
        try:
            return datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            return datetime.fromtimestamp(0, tz=timezone.utc)
    items.sort(key=keydate, reverse=True)

    # 5) BACKFILL: latest N items
    backfill_items = items[:BACKFILL_CAP]
    write_rss(BACKFILL_XML, backfill_items, SELF_RAW_BACKFILL)

    # 6) LIVE: only items never seen before
    seen = load_seen()
    new_items = [it for it in items if it["guid"] not in seen]

    # Update state with everything seen (so tomorrow only truly new episodes fire)
    for it in items:
        seen.add(it["guid"])
    save_seen(seen)

    live_emit = new_items[:LIVE_MAX_NEW]  # emit at most LIVE_MAX_NEW
    write_rss(LIVE_XML, live_emit, SELF_RAW_LIVE)


if __name__ == "__main__":
    main()
