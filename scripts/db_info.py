"""Inspect the Singularity SQLite database: list tables, row counts, and schema.

Usage:
    python scripts/db_info.py                # default: data/singularity.db
    python scripts/db_info.py data/test.db
"""

import sqlite3
import sys
from pathlib import Path


def show(db_path: str) -> None:
    path = Path(db_path)
    print(f"\n=== {path} ({'exists' if path.exists() else 'MISSING'}) ===")
    if not path.exists():
        return
    conn = sqlite3.connect(str(path))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    for t in tables:
        count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  {t:<20} rows={count}")
    if "--schema" in sys.argv:
        for t in tables:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()[0]
            print(f"\n--- {t} ---\n{sql}")
    conn.close()


if __name__ == "__main__":
    targets = sys.argv[1:] or [
        "data/singularity.db",
        "data/test.db",
    ]
    for t in targets:
        show(t)