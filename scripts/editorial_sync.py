#!/usr/bin/env python3
"""Incremental sync of editorial data to production.

Instead of uploading the entire 961MB SQLite database on every deploy,
this script exports only the editorial tables (articles, sources, tags,
skeet drafts, media images, notifications) as SQL and pipes it through
SSH to the production server's sqlite3 CLI.

Usage:
    ssh root@poliscopic.com "apt-get install -y sqlite3"  # once
    python scripts/editorial_sync.py                       # surgical push

The editorial tables are ~200 KB vs 961 MB for the full database —
roughly 5,000x smaller.
"""

import sqlite3
import sys

EDITORIAL_TABLES = [
    ("articles", "id"),
    ("article_sources", "id"),
    ("article_tags", None),
    ("skeet_drafts", "id"),
    ("tags", "id"),
    ("admin_notifications", "id"),
    ("media_images", "id"),
]


def generate_sql(db_path: str = "data/maricopa.sqlite") -> str:
    """Generate INSERT OR REPLACE statements for all editorial tables."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    parts = ["BEGIN TRANSACTION;\n"]

    for table, pk in EDITORIAL_TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        cols = [r[1] for r in info]

        for row in rows:
            vals = []
            for c in cols:
                v = row[c] if c in row.keys() else None
                if v is None:
                    vals.append("NULL")
                elif isinstance(v, (int, float)):
                    vals.append(str(v))
                else:
                    escaped = str(v).replace("'", "''")
                    vals.append(f"'{escaped}'")
            parts.append(
                f"INSERT OR REPLACE INTO {table} "
                f"({','.join(cols)}) VALUES ({','.join(vals)});\n"
            )

    parts.append("COMMIT;\n")
    conn.close()
    return "".join(parts)


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/maricopa.sqlite"
    sql = generate_sql(db_path)
    sql_size = len(sql.encode("utf-8"))
    print(f"Generated {sql_size / 1024:.1f} KB SQL", file=sys.stderr)
    sys.stdout.write(sql)


if __name__ == "__main__":
    main()
