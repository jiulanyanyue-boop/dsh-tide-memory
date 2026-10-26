"""SQLite 存储层：连接、schema、记忆读写。

只用标准库 sqlite3，零第三方依赖。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


# --------------------------------------------------------------------------- #
# 时间工具
# --------------------------------------------------------------------------- #

def utc_now() -> str:
    """当前时间，ISO8601 UTC，秒级。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ms_to_iso(ms: int | float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(
        microsecond=0
    ).isoformat()


# --------------------------------------------------------------------------- #
# 连接与建库
# --------------------------------------------------------------------------- #

def connect(db_path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    """打开数据库。':memory:' 也支持。"""
    if str(db_path) != ":memory:":
        path = Path(db_path).expanduser().resolve()
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
    else:
        conn = sqlite3.connect(":memory:")

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_columns(conn)
    conn.commit()


#: 迁移 002 需要补的列：(列名, 定义)
MIGRATION_COLUMNS: list[tuple[str, str]] = [
    ("scope", "TEXT NOT NULL DEFAULT 'global'"),
    ("scope_ref", "TEXT NOT NULL DEFAULT ''"),
    ("summary", "TEXT NOT NULL DEFAULT ''"),
    ("depends_on", "TEXT NOT NULL DEFAULT ''"),
    ("expired_by", "TEXT"),
    ("is_status", "INTEGER NOT NULL DEFAULT 0"),
]


def ensure_columns(conn: sqlite3.Connection) -> None:
    """老库补列：幂等，可反复调用。

    SQLite 的 ALTER TABLE ADD COLUMN 不支持带 CHECK 的约束，
    所以 expired_by 的取值校验放在应用层（见 memory_semantics）。
    另外老库的 status CHECK 里没有 'expired'，也不能改——
    这种情况下降级用 status='superseded' + expired_by 记录真实原因。

    索引也在这里建：老库跑 schema.sql 时新列还不存在，建索引会报错。
    """
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
    for name, decl in MIGRATION_COLUMNS:
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError:
            # 并发/已存在，忽略
            pass
    # 列齐了再建索引
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_scope "
            "ON memories(scope, scope_ref, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_expire "
            "ON memories(status, expired_by)"
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Memory:
    id: str
    kind: str
    subject: str
    predicate: str
    object: str
    content: str
    status: str = "active"
    valid_from: str = ""
    valid_to: str | None = None
    recorded_at: str = ""
    last_seen_at: str = ""
    confidence: float = 0.5
    supersedes: str | None = None
    superseded_by: str | None = None
    source_session: str | None = None
    source_seq: int | None = None
    fingerprint: str = ""
    created_at: str = ""
    updated_at: str = ""
    # 迁移 002
    scope: str = "global"
    scope_ref: str = ""
    summary: str = ""
    depends_on: str = ""
    expired_by: str | None = None
    is_status: int = 0


def row_to_memory(row: sqlite3.Row) -> Memory:
    keys = set(row.keys())
    data = {k: row[k] for k in keys if k in Memory.__dataclass_fields__}
    return Memory(**data)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #

@dataclass
class Store:
    conn: sqlite3.Connection
    run_id: str = ""
    _evidence_written: set[str] = field(default_factory=set, repr=False)

    # -- 查询 ------------------------------------------------------------- #

    def get(self, memory_id: str) -> Memory | None:
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return row_to_memory(row) if row else None

    def neighbors(
        self,
        *,
        kind: str,
        subject: str,
        predicate: str,
        include_superseded: bool = False,
        scope: str | None = None,
        scope_ref: str | None = None,
    ) -> list[Memory]:
        """同一 (kind, subject, predicate) 下的有效条目 + 候选池条目。

        scope / scope_ref：给了就**只在该归属内**查找邻居。
        这是必须的——否则项目 B 说「数据库用 MySQL」会把项目 A 的
        「数据库用 PostgreSQL」给 invalidate 掉，跨项目互相污染。
        """
        sql = (
            "SELECT * FROM memories "
            "WHERE kind = ? AND subject = ? AND predicate = ? "
            "AND status IN ('active', 'candidate') "
        )
        args: list[Any] = [kind, subject, predicate]
        if not include_superseded:
            sql += "AND valid_to IS NULL "
        # 归属隔离：None 表示「调用方没指定」，退回旧行为（兼容/迁移场景）
        if scope is not None:
            sql += "AND COALESCE(scope, 'global') = ? "
            args.append(scope or "global")
        if scope is not None and scope_ref:
            sql += "AND COALESCE(scope_ref, '') = ? "
            args.append(scope_ref)
        sql += "ORDER BY last_seen_at DESC"
        rows = self.conn.execute(sql, tuple(args)).fetchall()
        return [row_to_memory(r) for r in rows]

    def active_key(
        self,
        kind: str,
        subject: str,
        predicate: str,
        *,
        scope: str | None = None,
        scope_ref: str | None = None,
    ) -> Memory | None:
        return next(
            iter(
                self.neighbors(
                    kind=kind,
                    subject=subject,
                    predicate=predicate,
                    scope=scope,
                    scope_ref=scope_ref,
                )
            ),
            None,
        )

    def count(self, status: str | None = None) -> int:
        if status is None:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE status = ?", (status,)
            ).fetchone()
        return int(row["n"])

    # -- 写入 ------------------------------------------------------------- #

    def insert(self, mem: Memory) -> None:
        self.conn.execute(
            """
            INSERT INTO memories (
              id, kind, subject, predicate, object, content, status,
              valid_from, valid_to, recorded_at, last_seen_at, confidence,
              supersedes, superseded_by, source_session, source_seq,
              fingerprint, created_at, updated_at,
              scope, scope_ref, summary, depends_on, expired_by, is_status
            ) VALUES (
              :id, :kind, :subject, :predicate, :object, :content, :status,
              :valid_from, :valid_to, :recorded_at, :last_seen_at, :confidence,
              :supersedes, :superseded_by, :source_session, :source_seq,
              :fingerprint, :created_at, :updated_at,
              :scope, :scope_ref, :summary, :depends_on, :expired_by, :is_status
            )
            """,
            mem.__dict__,
        )
        self.conn.commit()

    # -- 迁移 002：过期 / 归属查询 ---------------------------------------- #

    def expire(self, memory_id: str, *, when: str, reason: str,
               new_id: str | None = None) -> None:
        """标失效但不删数据。status 优先用 'expired'，老库降级 'superseded'。"""
        try:
            self.conn.execute(
                "UPDATE memories SET status = 'expired', expired_by = ?, valid_to = ?, "
                "superseded_by = COALESCE(?, superseded_by), updated_at = ? WHERE id = ?",
                (reason, when, new_id, when, memory_id),
            )
        except sqlite3.IntegrityError:
            self.conn.execute(
                "UPDATE memories SET status = 'superseded', expired_by = ?, valid_to = ?, "
                "superseded_by = COALESCE(?, superseded_by), updated_at = ? WHERE id = ?",
                (reason, when, new_id, when, memory_id),
            )
        self.conn.commit()

    def reject(self, memory_id: str, *, when: str, reason: str) -> None:
        """标 rejected：抽取失败/不该记，保留审计痕迹，不物理删除。"""
        self.conn.execute(
            "UPDATE memories SET status = 'rejected', expired_by = ?, "
            "updated_at = ? WHERE id = ?",
            (reason, when, memory_id),
        )
        self.conn.commit()

    def by_scope(self, scope: str, scope_ref: str | None = None,
                 *, only_active: bool = True) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE scope = ?"
        args: list[Any] = [scope]
        if scope_ref is not None:
            sql += " AND scope_ref = ?"
            args.append(scope_ref)
        if only_active:
            sql += " AND status = 'active'"
        sql += " ORDER BY confidence DESC, last_seen_at DESC"
        return [row_to_memory(r) for r in self.conn.execute(sql, args).fetchall()]

    def with_dependencies(self) -> list[Memory]:
        """所有带 depends_on 的有效条目（用于依赖死亡检查）。"""
        return [
            row_to_memory(r)
            for r in self.conn.execute(
                "SELECT * FROM memories WHERE status = 'active' "
                "AND depends_on IS NOT NULL AND depends_on != ''"
            ).fetchall()
        ]

    def touch(self, memory_id: str, when: str, *, confidence: float | None = None) -> None:
        """刷新 last_seen_at，可选提升置信度。"""
        if confidence is None:
            self.conn.execute(
                "UPDATE memories SET last_seen_at = ?, updated_at = ? WHERE id = ?",
                (when, when, memory_id),
            )
        else:
            self.conn.execute(
                "UPDATE memories SET last_seen_at = ?, updated_at = ?, "
                "confidence = MIN(1.0, MAX(confidence, ?)) WHERE id = ?",
                (when, when, confidence, memory_id),
            )
        self.conn.commit()

    def merge(self, memory_id: str, *, object: str, content: str,
              confidence: float, when: str) -> None:
        """补充式更新：保留原条目，丰富内容并升置信度。"""
        self.conn.execute(
            "UPDATE memories SET object = ?, content = ?, "
            "confidence = MIN(1.0, confidence + ?), "
            "last_seen_at = ?, updated_at = ? WHERE id = ?",
            (object, content, confidence, when, when, memory_id),
        )
        self.conn.commit()

    def invalidate(self, memory_id: str, *, when: str, new_id: str) -> None:
        self.conn.execute(
            "UPDATE memories SET status = 'superseded', valid_to = ?, "
            "superseded_by = ?, expired_by = COALESCE(expired_by, 'superseded'), "
            "updated_at = ? WHERE id = ?",
            (when, new_id, when, memory_id),
        )
        self.conn.commit()

    def promote(self, memory_id: str, *, when: str, confidence: float) -> None:
        """候选池 -> 正式记忆（获得足够置信度后）。"""
        self.conn.execute(
            "UPDATE memories SET status = 'active', confidence = ?, "
            "last_seen_at = ?, updated_at = ? WHERE id = ?",
            (min(1.0, confidence), when, when, memory_id),
        )
        self.conn.commit()

    def add_evidence(self, memory_id: str, *, session_id: str | None,
                     seq: int | None, quote: str, recorded_at: str) -> None:
        """同一会话同一片段只记一次（幂等）。"""
        dedup = f"{memory_id}|{session_id}|{seq}"
        if dedup in self._evidence_written:
            return
        self._evidence_written.add(dedup)
        self.conn.execute(
            "INSERT INTO memory_evidence (memory_id, session_id, seq, quote, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (memory_id, session_id, seq, quote, recorded_at),
        )
        self.conn.commit()

    def evidence(self, memory_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM memory_evidence WHERE memory_id = ? ORDER BY id",
            (memory_id,),
        ).fetchall()

    # -- 审计 / 运行 ------------------------------------------------------ #

    def log(self, *, action: str, memory_id: str | None, target_id: str | None,
            candidate: Any, decision: Any, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log (run_id, ts, action, memory_id, target_id, "
            "candidate_json, decision_json, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                utc_now(),
                action,
                memory_id,
                target_id,
                json.dumps(candidate, ensure_ascii=False, sort_keys=True),
                json.dumps(decision, ensure_ascii=False, sort_keys=True),
                note,
            ),
        )
        self.conn.commit()

    def start_run(self) -> str:
        if not self.run_id:
            self.run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        self.conn.execute(
            "INSERT OR IGNORE INTO runs (run_id, started_at) VALUES (?, ?)",
            (self.run_id, utc_now()),
        )
        self.conn.commit()
        return self.run_id

    def finish_run(self, stats: dict[str, int]) -> None:
        if not self.run_id:
            return
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, stats_json = ? WHERE run_id = ?",
            (utc_now(), json.dumps(stats, ensure_ascii=False), self.run_id),
        )
        self.conn.commit()

    def audit(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if run_id:
            return self.conn.execute(
                "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
