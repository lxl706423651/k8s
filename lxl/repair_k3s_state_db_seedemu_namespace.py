#!/usr/bin/env python3
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

DB = Path("/var/lib/rancher/k3s/server/db/state.db")
NS = "seedemu-k3s-real-topo"
PATTERN = f"/registry/%{NS}%"
BACKUP_DIR = DB.parent / "backup_manual"


def main() -> int:
    if not DB.exists():
        print(f"state db not found: {DB}", file=sys.stderr)
        return 1

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"state.db.{ts}"
    shutil.copy2(DB, backup)
    print(f"backup={backup}")

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    total = cur.execute("select count(*) from kine").fetchone()[0]
    ns_rows = cur.execute("select count(*) from kine where name like ?", (PATTERN,)).fetchone()[0]
    compact_rev = cur.execute(
        "select max(prev_revision) from kine where name=?",
        ("compact_rev_key",),
    ).fetchone()[0]
    ns_after_compact = cur.execute(
        "select count(*) from kine where id > ? and name like ?",
        (compact_rev, PATTERN),
    ).fetchone()[0]
    print(f"before_total={total}")
    print(f"before_ns_rows={ns_rows}")
    print(f"compact_rev={compact_rev}")
    print(f"before_ns_after_compact={ns_after_compact}")

    cur.execute("begin immediate")
    cur.execute("delete from kine where name like ?", (PATTERN,))
    deleted = cur.rowcount
    conn.commit()
    print(f"deleted_rows={deleted}")

    cur.execute("vacuum")
    conn.commit()

    after_total = cur.execute("select count(*) from kine").fetchone()[0]
    after_ns_rows = cur.execute("select count(*) from kine where name like ?", (PATTERN,)).fetchone()[0]
    print(f"after_total={after_total}")
    print(f"after_ns_rows={after_ns_rows}")
    conn.close()
    print(f"final_size_bytes={DB.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
