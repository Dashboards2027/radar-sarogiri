import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB = Path(__file__).with_name("radar.db")

CATEGORIES = {
    "Deportes": r"\bvs\b|futbol|gran premio|\bf1\b|\bnba\b|\bnfl\b|packers|boca|river|copa|liga|partido|\bgol\b|tenis|\bufc\b|messi|colapinto|seleccion|mundial|rugby|nrl|cbf|amistoso|referee|\bge\b|getv|rublev|andreeva|o'connell|alford",
    "Música y Entretenimiento": r"taylor|concierto|album|tour|serie|pelicula|netflix|popstars|festival|oscar|grammy|kpop|\bshow\b|estreno|globo ?play|promesa|stream|配信|spotify|cantante|actor|actriz|rollers|callard|oceans calling|reality",
    "IA y Tech": r"\bai\b|\bia\b|chatgpt|\bgpt|openai|gemini|claude|iphone|apple|android|\bapp\b|prompt|robot|tesla|steam|superintelligence|data center|nvidia|tiktok|instagram|whatsapp|gaming|\bgame|playstation|xbox",
    "Belleza y Moda": r"makeup|maquillaje|skincare|\bpelo\b|\bhair|moda|fashion|outfit|blumarine|ss2\d|\blook|desfile|perfume|kerastase|beauty|belleza|nails|unas|zapatillas|sneaker",
    "Comida y Bebida": r"receta|comida|food|\bcafe|burger|pizza|chicken|チキン|vino|cerveza|restaurante|mcdonald|starbucks|mate|asado|palte|plate|cocina|chef|helado|dulce",
    "Bienestar y Fitness": r"running|\brun\b|gym|yoga|salud|wellness|caminar|maraton|dieta|sleep|meditacion|fitness|entrenamiento|pasito|cold plunge|longevity",
    "Noticias y Política": r"israel|trump|milei|gobierno|elecciones|crisis|estafa|\bley\b|guerra|presidente|congreso|migratori|dolar|clima|beca|trabajador|prince william|sancho|melilla|lotofacil|juicio|policia",
}
TIKTOK_INDUSTRY = {
    "Food & Beverage": "Comida y Bebida",
    "Apparel & Accessories": "Belleza y Moda",
    "Beauty & Personal Care": "Belleza y Moda",
    "News & Entertainment": "Música y Entretenimiento",
    "Sports & Outdoor": "Deportes",
    "Games": "IA y Tech",
    "Tech & Electronics": "IA y Tech",
    "Education": "Cultura y Memes",
    "Pets": "Cultura y Memes",
    "Travel": "Cultura y Memes",
    "Life": "Bienestar y Fitness",
}
SOURCE_CATEGORY = {
    "Memes": "Cultura y Memes",
    "Mercado Libre": "Consumo y Productos",
    "YouTube": "Música y Entretenimiento",
    "Shorts": "Música y Entretenimiento",
    "Instagram": "Música y Entretenimiento",
    "Spotify": "Música y Entretenimiento",
}
EXPERIENCE_FRIENDLY = {"Consumo y Productos", "Belleza y Moda", "Comida y Bebida", "Bienestar y Fitness", "IA y Tech", "Música y Entretenimiento", "Cultura y Memes"}


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode() or s.lower()
    return re.sub(r"[^a-z0-9぀-鿿]+", "", s)


def categorize(text):
    t = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode() + " " + text.lower()
    for cat, pattern in CATEGORIES.items():
        if re.search(pattern, t):
            return cat
    if text.startswith("#") or "what is going on" in t or "meme" in t or "what's going on" in t:
        return "Cultura y Memes"
    return "Otros"


MONTHS = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}


def days_since(spanish_date):
    """'17 sept 2026' -> days ago."""
    m = re.match(r"(\d{1,2}) ([a-z]{3})\w*\.? (\d{4})", spanish_date.lower())
    if not m or m.group(2) not in MONTHS:
        return None
    return (date.today() - date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))).days


def stage_for(t):
    if t["source"] == "X":
        h = t["extra"].get("hours_in_trending", 0)
        if h <= 2:
            return "Emergente"
        if h <= 6:
            return "Creciendo"
        if h <= 14:
            return "Pico"
        return "Saturado"
    if t["source"] == "Reddit":
        # "rising" already means climbing; only the very top is genuinely starting
        return "Emergente" if t["rank"] <= 5 else "Creciendo" if t["rank"] <= 12 else "Pico"
    if t["source"] == "TikTok":
        views = t["extra"].get("views", "")
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(views[-1:], 1)
        n = float(re.sub(r"[^\d.]", "", views) or 0) * mult
        if t["first_seen_hours"] <= 6 and n < 50e6:
            return "Emergente"
        return "Pico" if n >= 100e6 else "Creciendo"
    if t["source"] in ("Shorts", "Spotify"):
        e = t["extra"]
        days = e.get("days_on_chart", 99)
        prev = e.get("rank_prev_day")
        climbed = (prev - t["rank"]) if prev else 0
        if e.get("is_new") or e.get("change", "").upper() in ("NEW", "RE") or days <= 3:
            return "Emergente"
        climbed = max(climbed, int(e.get("change", "+0")[1:] or 0) if e.get("change", "").startswith("+") else 0)
        if climbed >= 15 or days <= 7:
            return "Creciendo"
        return "Saturado" if days > 45 else "Pico"
    if t["source"] == "Pinterest":
        wow, mom = t["extra"].get("wow", 0), t["extra"].get("mom", 0)
        return "Emergente" if wow >= 30 else "Creciendo" if mom >= 50 else "Pico"
    if t["source"] == "Instagram":
        m = t["extra"].get("momentum", "").lower()
        return "Emergente" if m in ("new", "nuevo") else "Creciendo" if m == "rising" else "Pico"
    if t["source"] == "YouTube":
        age = days_since(t["extra"].get("released", ""))
        if age is None:
            return "Pico"
        return "Emergente" if age <= 3 else "Creciendo" if age <= 10 else "Pico"
    if t["source"] in ("Mercado Libre", "Wikipedia"):
        # popularity lists, not momentum: only brand-new entries near the top count as growing
        return "Creciendo" if t["first_seen_hours"] <= 24 and t["rank"] <= 5 else "Pico"
    if t["source"] in ("Memes", "Bluesky"):
        h = t["first_seen_hours"]
        if h <= 6 and t["rank"] <= 8:
            return "Emergente"
        return "Creciendo" if h <= 24 else "Pico"
    traffic = int(re.sub(r"\D", "", t["traffic"] or "0") or 0) * (1000 if "K" in t["traffic"] else 1_000_000 if "M" in t["traffic"] else 1)
    started = t["extra"].get("started_hours_ago")
    if started is not None:
        if not t["extra"].get("active", True):
            return "Saturado"
        if started <= 4:
            return "Emergente"
        return "Creciendo" if started <= 12 and traffic < 500_000 else "Pico"
    if t["first_seen_hours"] <= 3 and traffic < 10000:
        return "Emergente"
    if traffic >= 10000:
        return "Pico"
    return "Creciendo"


def load_latest(conn):
    rows = conn.execute(
        """SELECT s.source, s.region, s.keyword, s.rank, s.traffic, s.url, s.extra, s.fetched_at,
                  (SELECT MIN(fetched_at) FROM snapshots f
                    WHERE f.keyword = s.keyword AND f.source = s.source AND f.region = s.region) AS first_seen
           FROM snapshots s
           JOIN (SELECT source, region, MAX(fetched_at) AS m FROM snapshots GROUP BY source, region) l
             ON s.source = l.source AND s.region = l.region AND s.fetched_at = l.m"""
    ).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for source, region, kw, rank, traffic, url, extra, fetched, first_seen in rows:
        out.append({
            "source": source, "region": region, "keyword": kw, "rank": rank,
            "traffic": traffic or "", "url": url or "", "extra": json.loads(extra or "{}"),
            "fetched_at": fetched,
            "first_seen_hours": round((now - datetime.fromisoformat(first_seen)).total_seconds() / 3600, 1),
        })
    return out


def cross_platform(trends):
    for t in trends:
        hits = {f"{o['source']} {o['region']}" for o in trends if o is t or same_trend(t["keyword"], o["keyword"])}
        t["seen_in"] = sorted(hits)


def score(t):
    s = max(0, 60 - t["rank"])
    s += {"Emergente": 35, "Creciendo": 20, "Pico": 8, "Saturado": 0}[t["stage"]]
    platforms = {p.split()[0] for p in t["seen_in"]}
    s += 25 * (len(platforms) - 1) + 5 * (len(t["seen_in"]) - 1)
    prev = t["extra"].get("rank_prev_hour")
    if t["source"] == "X" and prev and prev > t["rank"]:
        s += min(20, (prev - t["rank"]) * 2)
    if t["category"] in EXPERIENCE_FRIENDLY:
        s += 10
    return s


def words(s):
    s = unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode() or s.lower()
    return tuple(re.findall(r"[a-z0-9]+", s))


def same_trend(a, b):
    """Same trend if the compact text matches, or the shorter phrase appears as whole words in a short-ish longer one."""
    ka, kb = norm(a), norm(b)
    if len(ka) >= 4 and ka == kb:
        return True
    wa, wb = words(a), words(b)
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if not short or len("".join(short)) < 5:
        return False
    if len(short) == 1 and len(long_) > 4:
        return False
    n = len(short)
    return any(long_[i : i + n] == short for i in range(len(long_) - n + 1))


def cluster(trends):
    """Merge items that name the same trend across sources/regions into one card."""
    parent = list(range(len(trends)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(trends)):
        for j in range(i + 1, len(trends)):
            if same_trend(trends[i]["keyword"], trends[j]["keyword"]):
                parent[find(i)] = find(j)
    groups = {}
    for i, t in enumerate(trends):
        groups.setdefault(find(i), []).append(t)
    cards = []
    for members in groups.values():
        members.sort(key=lambda t: -t["score"])
        lead = dict(members[0])
        lead["members"] = [
            {k: m[k] for k in ("source", "region", "keyword", "rank", "traffic", "url", "stage")}
            for m in members
        ]
        lead["seen_in"] = sorted({f"{m['source']} {m['region']}" for m in members})
        lead["platforms"] = sorted({m["source"] for m in members})
        lead["jumped"] = len(lead["platforms"]) > 1
        cats = [m["category"] for m in members if m["category"] != "Otros"]
        lead["category"] = max(set(cats), key=cats.count) if cats else "Otros"
        lead["experience_friendly"] = lead["category"] in EXPERIENCE_FRIENDLY
        lead["score"] = members[0]["score"] + 4 * (len(members) - 1)
        # Sarogiri: ~95% of what they use is Argentina + X; foreign politics is noise for them.
        in_ar = any(m["region"] == "AR" for m in members)
        if in_ar:
            lead["score"] += 30
        if "X" in lead["platforms"]:
            lead["score"] += 15
        if lead["category"] == "Noticias y Política" and not in_ar:
            lead["score"] -= 40
        cards.append(lead)
    return cards


HISTORY_HOURS = 72


def attach_history(conn, cards):
    """Rank over time for each card's main source, plus a simple direction for the trend sheet."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=HISTORY_HOURS)).isoformat(timespec="seconds")
    series = {}
    for source, region, kw, rank, at in conn.execute(
        "SELECT source, region, keyword, rank, fetched_at FROM snapshots WHERE fetched_at >= ? ORDER BY fetched_at", (cutoff,)
    ):
        series.setdefault((source, region, kw), []).append((at, rank))
    for t in cards:
        pts = series.get((t["source"], t["region"], t["keyword"]), [])
        t["history"] = [[at[:16], r] for at, r in pts][-HISTORY_HOURS:]
        ranks = [r for _, r in pts]
        if len(ranks) < 3:
            t["direction"] = "nuevo"
        else:
            recent = sum(ranks[-3:]) / 3
            before = sum(ranks[-6:-3]) / len(ranks[-6:-3]) if len(ranks) >= 6 else ranks[0]
            t["direction"] = "sube" if recent < before - 1 else "baja" if recent > before + 1 else "estable"
        t["window"] = opportunity_window(t)


def opportunity_window(t):
    """Rough time left to use the trend before it feels late, from its stage and recent direction."""
    stage, d = t["stage"], t.get("direction")
    if stage == "Saturado":
        return "Ya pasó el mejor momento"
    if d == "baja":
        return "1 a 2 días (está bajando)"
    return {
        "Emergente": "7 a 10 días" if d in ("sube", "nuevo") else "5 a 7 días",
        "Creciendo": "4 a 7 días" if d == "sube" else "3 a 5 días",
        "Pico": "1 a 3 días",
    }.get(stage, "")


def analyze(generate=False):
    if not DB.exists():
        return {"updated_at": None, "trends": []}
    conn = sqlite3.connect(DB)
    trends = load_latest(conn)
    for t in trends:
        t["category"] = SOURCE_CATEGORY.get(t["source"]) or categorize(t["keyword"] + " " + t["extra"].get("news", ""))
        if t["category"] in ("Otros", "Cultura y Memes") and t["extra"].get("industry") in TIKTOK_INDUSTRY:
            t["category"] = TIKTOK_INDUSTRY[t["extra"]["industry"]]
        t["stage"] = stage_for(t)
    cross_platform(trends)
    for t in trends:
        t["jumped"] = len({p.split()[0] for p in t["seen_in"]}) > 1
        t["experience_friendly"] = t["category"] in EXPERIENCE_FRIENDLY
        t["score"] = score(t)
    cards = cluster(trends)
    cards.sort(key=lambda t: -t["score"])
    attach_history(conn, cards)
    conn.close()
    from explain import enrich
    made = enrich(cards, generate=generate)
    if generate:
        print(f"Explicaciones nuevas con IA: {made}")
    return {"updated_at": max((t["fetched_at"] for t in trends), default=None), "trends": cards}


def export_static(path=Path(__file__).with_name("data.js")):
    """Lets dashboard.html work when opened directly from disk, without the server."""
    payload = json.dumps(analyze(generate=True), ensure_ascii=True).replace("</", "<\\/")
    path.write_text(f"window.RADAR_DATA = {payload};\n", encoding="utf-8")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for t in analyze()["trends"][:25]:
        print(f"{t['score']:>4} {t['stage']:<10} {t['category']:<24} {t['source']} {t['region']:<7} {t['keyword'][:60]}  {t['seen_in'] if t['jumped'] else ''}")
