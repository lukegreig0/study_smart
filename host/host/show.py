"""`python -m host.show`: print the current state of every desk.

Opens the database read-only, so it's safe to run while the host is writing
(WAL mode lets readers see a consistent snapshot).
"""

from __future__ import annotations

import os
import sqlite3
import sys

from .store import Store

COLUMNS = ("space", "device", "node", "state", "since")


def format_table(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(COLUMNS[i]), *(len(r[i]) for r in rows)) for i in range(len(COLUMNS))]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()
    out = [line(COLUMNS), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else os.environ.get("DB_PATH", "./studyspace.db")
    try:
        store = Store.connect(path, readonly=True)
    except sqlite3.Error as exc:
        print(f"cannot open {path}: {exc}", file=sys.stderr)
        return 1
    try:
        devices = store.list_devices()
    except sqlite3.Error as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    if not devices:
        print(f"no devices in {path}")
        return 0
    rows = [(d.space_id, d.device_id, d.node_id, d.state, d.since or "-") for d in devices]
    print(format_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
