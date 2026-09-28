import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import defusedxml.ElementTree as ET

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

X_REGIONS = {"AR": "argentina/", "MUNDO": "", "US": "united-states/"}
GOOGLE_GEOS = ["AR", "US", "GB", "BR", "MX", "ES"]
TIKTOK_COUNTRIES = ["AR", "US", "MX", "BR", "ES", "GB"]
REDDIT_SUBS = {"MUNDO": "all", "AR": "argentina", "EXPLICA": "OutOfTheLoop"}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def item(source, region, keyword, rank, traffic="", url="", extra=None):
    return {
        "source": source,
        "region": region,
        "keyword": keyword.strip(),
        "rank": rank,
        "traffic": traffic,
        "url": url,
        "extra": extra or {},
    }


def x_trends(region):
    """trends24 shows one list per hour; we use them to get hours-in-trending and rank trajectory."""
    page = _get(f"https://trends24.in/{X_REGIONS[region]}")
    blocks = re.findall(r"<ol class=trend-card__list>(.*?)</ol>", page, re.S)[:24]
    hourly = [
        [html.unescape(t) for t in re.findall(r"class=trend-link>([^<]+)<", b)] for b in blocks
    ]
    if not hourly:
        return []
    out = []
    for rank, kw in enumerate(hourly[0], 1):
        ranks = [h.index(kw) + 1 if kw in h else None for h in hourly]
        hours_seen = sum(r is not None for r in ranks)
        prev = next((r for r in ranks[1:] if r is not None), None)
        out.append(
            item(
                "X",
                region,
                kw,
                rank,
                url="https://x.com/search?q=" + urllib.parse.quote(kw),
                extra={
                    "hours_in_trending": hours_seen,
                    "is_new": hours_seen == 1,
                    "rank_prev_hour": prev,
                    "rank_history": ranks[:12],
                },
            )
        )
    return out


def google_trends(geo):
    root = ET.fromstring(_get(f"https://trends.google.com/trending/rss?geo={geo}"))
    ns = {"ht": "https://trends.google.com/trending/rss"}
    out = []
    for rank, i in enumerate(root.iter("item"), 1):
        news = i.find("ht:news_item", ns)
        out.append(
            item(
                "Google",
                geo,
                i.findtext("title"),
                rank,
                traffic=i.findtext("ht:approx_traffic", namespaces=ns) or "",
                url=news.findtext("ht:news_item_url", namespaces=ns) if news is not None else "",
                extra={"news": news.findtext("ht:news_item_title", namespaces=ns) if news is not None else ""},
            )
        )
    return out


def reddit_rising(region):
    atom = {"a": "http://www.w3.org/2005/Atom"}
    url = f"https://www.reddit.com/r/{REDDIT_SUBS[region]}/rising/.rss?limit=25"
    for wait in (8, 30, 60):  # Reddit answers 429 to back-to-back unauthenticated requests
        time.sleep(wait)
        try:
            root = ET.fromstring(_get(url))
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or wait == 60:
                raise
    out = []
    for rank, e in enumerate(root.findall("a:entry", atom), 1):
        cat = e.find("a:category", atom)
        link = e.find("a:link", atom)
        out.append(
            item(
                "Reddit",
                region,
                e.findtext("a:title", namespaces=atom),
                rank,
                url=link.get("href") if link is not None else "",
                extra={"subreddit": cat.get("label") if cat is not None else ""},
            )
        )
    return out


def _parse_tiktok(text, region):
    """Creative Center lists: rank, #tag, [industry], posts, 'Posts', views, 'Views' (industry is sometimes absent)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines):
        if not (line.startswith("#") and i > 0 and lines[i - 1].isdigit()):
            continue
        block = lines[i + 1 : i + 7]
        posts = block[block.index("Posts") - 1] if "Posts" in block else ""
        views = block[block.index("Views") - 1] if "Views" in block else ""
        industry = block[0] if block and not re.match(r"^[\d.,]+[KMB]?$", block[0]) else ""
        out.append(
            item(
                "TikTok",
                region,
                line,
                int(lines[i - 1]),
                traffic=f"{views} vistas" if views else "",
                url="https://www.tiktok.com/tag/" + urllib.parse.quote(line.lstrip("#")),
                extra={"industry": industry, "posts": posts, "views": views},
            )
        )
    return out


def _hours_ago(text):
    m = re.search(r"(\d+)\s*(min|hour|hora|day|d[ií]a)", text, re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return round(n / 60, 1) if unit.startswith("min") else n * 24 if unit[0] == "d" else n


def _parse_google_now(text, geo):
    """Google Trends 'Trending now' rows: title, volume (200K+), arrow, growth %, 'N hours ago', icon, status, related..."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines):
        if not (re.fullmatch(r"\d[\d.,]*\s?[KMkm]?\+", line) and i > 0 and i + 4 < len(lines)):
            continue
        if "arrow" not in lines[i + 1]:
            continue
        title = lines[i - 1]
        growth = lines[i + 2] if "%" in lines[i + 2] else ""
        started = next((l for l in lines[i + 2 : i + 5] if _hours_ago(l) is not None), "")
        status_idx = next((j for j in range(i + 3, min(i + 7, len(lines))) if lines[j] in ("Activa", "Active", "Duró", "Lasted")), None)
        active = status_idx is not None and lines[status_idx] in ("Activa", "Active")
        related = []
        if status_idx is not None:
            for l in lines[status_idx + 1 : status_idx + 6]:
                if re.fullmatch(r"\d[\d.,]*\s?[KMkm]?\+", l) or l.startswith("y ") or l.startswith("+ "):
                    break
                related.append(l)
            related = related[:-1] if related else related  # last one is the next row's title
        out.append(
            item(
                "Google",
                geo,
                title,
                len(out) + 1,
                traffic=line,
                url="https://www.google.com/search?q=" + urllib.parse.quote(title),
                extra={
                    "growth": growth,
                    "started_hours_ago": _hours_ago(started),
                    "active": active,
                    "news": ", ".join(related[:3]),
                },
            )
        )
    return out


def browser_sources(tiktok_countries=TIKTOK_COUNTRIES, google_geos=GOOGLE_GEOS):
    """TikTok Creative Center and Google Trends 'Trending now' only render in a real browser."""
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(locale="es-AR", user_agent=UA, viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        for cc in tiktok_countries:
            try:
                page.goto(
                    f"https://ads.tiktok.com/creative/creativeCenter/trends/hashtag?countryCode={cc}&period=7&region={cc}",
                    timeout=60000,
                )
                page.get_by_text("Posts", exact=True).first.wait_for(timeout=30000)
                results[f"TikTok {cc}"] = _parse_tiktok(page.locator("body").inner_text(), cc)
            except Exception as e:
                results[f"TikTok {cc}"] = f"FALLA: {type(e).__name__}: {e}"
        for geo in google_geos:
            try:
                page.goto(f"https://trends.google.com/trending?geo={geo}&hours=24", timeout=60000)
                page.wait_for_function("document.body.innerText.includes('arrow_upward')", timeout=30000)
                page.wait_for_timeout(1500)
                items = _parse_google_now(page.locator("body").inner_text(), geo)
                if not items:
                    raise ValueError("sin filas")
                results[f"Google {geo}"] = items
            except Exception:
                try:
                    results[f"Google {geo}"] = google_trends(geo)  # RSS fallback
                except Exception as e:
                    results[f"Google {geo}"] = f"FALLA: {type(e).__name__}: {e}"
        browser.close()
    return results


def know_your_meme():
    page = _get("https://knowyourmeme.com/memes/submissions")
    out = []
    for href, img, title in re.findall(
        r'<a class="result" href="(/memes/[^"/]+)">\s*<img[^>]*?src="([^"]+)"[^>]*>\s*<span>\s*(.*?)\s*</span>', page, re.S
    ):
        out.append(
            item("Memes", "MUNDO", html.unescape(title), len(out) + 1,
                 url="https://knowyourmeme.com" + href, extra={"image": img})
        )
    return out[:25]


def bluesky_trending():
    data = json.loads(_get("https://public.api.bsky.app/xrpc/app.bsky.unspecced.getTrendingTopics?limit=25"))
    return [
        item("Bluesky", "MUNDO", t.get("displayName") or t["topic"], rank,
             url="https://bsky.app" + t.get("link", ""), extra={"news": t.get("description", "")})
        for rank, t in enumerate(data.get("topics", []), 1)
    ]


def all_sources():
    jobs = [(f"X {r}", lambda r=r: x_trends(r)) for r in X_REGIONS]
    jobs += [("Memes (Know Your Meme)", know_your_meme), ("Bluesky", bluesky_trending)]
    jobs += [(f"Reddit {r}", lambda r=r: reddit_rising(r)) for r in REDDIT_SUBS]
    return jobs
