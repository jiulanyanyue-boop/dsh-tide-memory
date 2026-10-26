"""导出检索快照：给 DSH 插件读的只读 JSON。

为什么需要它：
  插件跑在 DSH 宿主进程里，那个进程不一定是 Node 24（可能是 Electron 内置 Node），
  未必有 node:sqlite。所以把「有效记忆」导成一个小 JSON，插件直接读文件即可，
  零数据库依赖，也不用管 WAL 锁。

  快照只是检索用的副本，真源永远是 SQLite。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from store import utc_now

SNAPSHOT_NAME = "memories.json"


def snapshot_path_for(db_path: str | Path) -> Path:
    return Path(db_path).expanduser().resolve().parent / SNAPSHOT_NAME


def write_snapshot(conn: sqlite3.Connection, *, path: str | Path) -> Path:
    target = Path(path)
    rows = conn.execute(
        "SELECT id, kind, subject, predicate, object, content, confidence, last_seen_at, "
        "scope, scope_ref, summary, depends_on "
        "FROM memories WHERE status = 'active' AND valid_to IS NULL "
        "ORDER BY last_seen_at DESC"
    ).fetchall()
    payload = {
        "generated_at": utc_now(),
        "count": len(rows),
        "items": [dict(row) for row in rows],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
