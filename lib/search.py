"""检索层：给巩固后的记忆做召回。

为什么不是纯 FTS5：
  SQLite 的 unicode61 分词器几乎不切中文（整段中文常被当成一个 token），
  所以中文查询用 FTS5 会大面积漏召回。这里做双通道：
    1) FTS5 MATCH —— 英文单词、空格分隔的短语
    2) 2-gram 重叠 —— 中文兜底
  再叠加时间衰减（recency），避免「旧偏好压新偏好」。

记忆库是个人的，量级几千条，线性扫描完全够用。
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from consolidate import _tokens, normalize, similarity


def _age_days(iso_ts: str, now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return 365.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 86400.0)


def _is_ascii_word(query: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\- ]+", query))


def coverage(query: str, doc: str) -> float:
    """查询覆盖率：查询的 bigram 有多少出现在文档里。

    为什么不用 Jaccard：分母是两边并集，长记忆的 bigram 多，
    哪怕全部命中查询的 8 个 bigram，sim 上限也只有 8/(60+8-8)=0.13。
    实测「记忆有什么优化空间」对一条 60 字记忆只拿到 0.0104，
    低于 0.02 阈值，导致刚存的记忆召不回来。覆盖率与文档长度无关。
    """
    tq = _tokens(query)
    if not tq:
        return 0.0
    td = _tokens(doc)
    return len(tq & td) / len(tq)


#: 疑问词/功能词：它们不承载检索意图，留在分母里只会稀释覆盖率
_QUESTION_GRAMS = {
    "什么", "有什", "么优", "怎么", "如何", "哪些", "是不", "不是",
    "可以", "能否", "是否", "我的", "我们", "这个", "那个", "一下",
}


def _core_query_tokens(query: str) -> set[str]:
    """去掉疑问词后的查询 bigram（用于覆盖率的分母）。"""
    tq = _tokens(query)
    core = tq - _QUESTION_GRAMS
    return core or tq


def coverage_core(query: str, doc: str) -> float:
    q = _core_query_tokens(query)
    if not q:
        return 0.0
    return len(q & _tokens(doc)) / len(q)


def token_hit_ratio(query: str, doc: str) -> float:
    """查询的任意 bigram 命中数比例（不看顺序、不看长度）。"""
    tq = _tokens(query)
    if not tq:
        return 0.0
    return len(tq & _tokens(doc)) / len(tq)


def visible_in_scope(row, visible_refs: list[str] | None, workspace_ref_val: str | None) -> bool:
    """这条记忆在当前会话里可见吗？

    global    → 永远可见
    workspace → scope_ref 必须等于当前工作区
    project   → scope_ref 必须命中当前项目身份（path: 和 git: 都接受）
    未知 scope → 不可见（fail-closed）

    visible_refs 为 None 表示「调用方没提供 cwd」，此时**只放行 global**——
    宁可少召回，也不能把别的项目的东西漏出去。
    """
    scope = (row["scope"] if "scope" in row.keys() else None) or "global"
    if scope == "global":
        return True

    if scope == "workspace":
        if not workspace_ref_val:
            return False
        ref = (row["scope_ref"] if "scope_ref" in row.keys() else "") or ""
        return ref == workspace_ref_val

    if scope == "project":
        if not visible_refs:
            return False
        ref = (row["scope_ref"] if "scope_ref" in row.keys() else "") or ""
        return ref in visible_refs

    return False


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 10,
    half_life_days: float = 30.0,
    semantic_weight: float = 0.75,
    min_score: float = 0.06,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """返回带 score 的记忆列表（只含仍有效的条目）。

    cwd：当前会话工作目录。给了就按归属过滤（项目 A 的记忆不会出现在项目 B）；
         不给则只返回 global 记忆——**fail-closed**。
    """
    q = (query or "").strip()
    if not q:
        return []

    # 解析当前会话的可见身份
    visible_refs: list[str] | None = None
    workspace_ref_val: str | None = None
    if cwd:
        from memory_semantics import known_project_refs, workspace_ref

        visible_refs = known_project_refs(cwd)
        workspace_ref_val = workspace_ref(cwd)

    rows = conn.execute(
        """
        SELECT m.id, m.kind, m.subject, m.predicate, m.object, m.content,
               m.confidence, m.valid_from, m.last_seen_at, m.summary,
               m.scope, m.scope_ref, m.depends_on
        FROM memories m
        WHERE m.status = 'active' AND m.valid_to IS NULL
        """
    ).fetchall()

    fts_ids: set[str] = set()
    if _is_ascii_word(q):
        # FTS5 查询按字面短语处理，避免把 * / OR / NEAR 当语法
        phrase = '"' + q.replace('"', '""') + '"'
        try:
            hit = conn.execute(
                "SELECT m.id FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
                "WHERE memories_fts MATCH ? "
                "AND m.status = 'active' AND m.valid_to IS NULL "
                "LIMIT 200",
                (phrase,),
            ).fetchall()
            fts_ids = {r["id"] for r in hit}
        except sqlite3.OperationalError:
            fts_ids = set()

    results: list[dict[str, Any]] = []
    hidden = 0
    for row in rows:
        # 归属过滤：项目 A 的记忆不能进项目 B
        if not visible_in_scope(row, visible_refs, workspace_ref_val):
            hidden += 1
            continue
        haystack = (
            f"{row['content']} {row['object']} {row['predicate']} "
            f"{row['summary'] or ''}"
        )
        # 四个通道取最大：原始 Jaccard（短查询好用）/ 覆盖率 / 覆盖率(去疑问词) / 命中比
        sim = max(
            similarity(q, haystack),
            coverage(q, haystack),
            coverage_core(q, haystack),
            token_hit_ratio(q, haystack) * 0.6,
        )
        if row["id"] in fts_ids:
            sim = max(sim, 0.8)

        if sim <= min_score:
            continue

        recency = math.exp(-_age_days(row["last_seen_at"]) / half_life_days)
        score = semantic_weight * sim + (1 - semantic_weight) * recency
        results.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "predicate": row["predicate"],
                "object": row["object"],
                "content": row["content"],
                "summary": row["summary"] or row["content"],
                "scope": row["scope"],
                "scope_ref": row["scope_ref"],
                "confidence": row["confidence"],
                "last_seen_at": row["last_seen_at"],
                "similarity": round(sim, 4),
                "recency": round(recency, 4),
                "score": round(score, 4),
            }
        )

    results.sort(key=lambda r: (r["score"], r["confidence"]), reverse=True)
    return results[:limit]


def search_with_embeddings(
    conn: sqlite3.Connection,
    query_vector: list[float],
    *,
    limit: int = 10,
    half_life_days: float = 30.0,
) -> list[dict[str, Any]]:
    """留好的向量通道：接入后只需在这里做余弦相似度。

    记忆向量建议单独存一张 embeddings 表（memory_id, dim, vec BLOB），
    或直接用 sqlite-vec 扩展。本文件不引入第三方依赖，故留桩。
    """
    raise NotImplementedError(
        "向量通道未接入：可用 sqlite-vec / LanceDB 存 memory_id 对应的向量，"
        "在这里做 cosine 后用与 search() 相同的方式叠加 recency。"
    )
