import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sources import all_sources, browser_sources

DB = Path(__file__).with_name("radar.db")
KEEP_DAYS = 7


def init(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            source TEXT NOT NULL,
            region TEXT NOT NULL,
            keyword TEXT NOT NULL,
            rank INTEGER,
            traffic TEXT,
            url TEXT,
            extra TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_kw ON snapshots(keyword, source, region)")


def save(conn, now, items):
    conn.executemany(
        "INSERT INTO snapshots (fetched_at, source, region, keyword, rank, traffic, url, extra)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            (now, i["source"], i["region"], i["keyword"], i["rank"], i["traffic"], i["url"],
             json.dumps(i["extra"], ensure_ascii=False))
            for i in items
        ],
    )


def run():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(DB)
    init(conn)
    report = {}
    for name, fetch in all_sources():
        try:
            items = fetch()
            save(conn, now, items)
            report[name] = items
        except Exception as e:
            report[name] = f"FALLA: {type(e).__name__}: {e}"
    try:
        browser = browser_sources()
    except Exception as e:
        browser = {"Navegador": f"FALLA: {type(e).__name__}: {e}"}
    for name, items in browser.items():
        if isinstance(items, str):
            report[name] = items
            continue
        save(conn, now, items)
        report[name] = items
    conn.execute(
        "DELETE FROM snapshots WHERE fetched_at < ?",
        ((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),),
    )
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    from analyze import export_static
    export_static()
    return report


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for name, res in run().items():
        if isinstance(res, str):
            print(f"\n=== {name}: {res}")
            continue
        print(f"\n=== {name}: {len(res)} items")
        for i in res[:6]:
            tag = ""
            if i["source"] == "X":
                e = i["extra"]
                tag = " [NUEVO]" if e["is_new"] else f" ({e['hours_in_trending']}h, antes #{e['rank_prev_hour']})"
            elif i["source"] == "Google" and "growth" in i["extra"]:
                e = i["extra"]
                tag = f" ({i['traffic']}, {e['growth']}, hace {e['started_hours_ago']}h, {'activa' if e['active'] else 'terminada'})"
            elif i["traffic"]:
                tag = f" ({i['traffic']})"
            print(f"  {i['rank']:>2}. {i['keyword'][:90]}{tag}")
