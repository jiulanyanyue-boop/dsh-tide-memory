"""把记忆库导出成人可读、可 git 管理的 Markdown。

约定
  memories/<kind>/<subject>-<predicate>-<id>.md   每条记忆一个文件
  memories/INDEX.md                               总览

内容不变就不重写，避免 git 里出现假的 dirty 状态。
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from store import Memory, row_to_memory

FRONTMATTER_KEYS = (
    "id", "kind", "subject", "predicate", "object", "status",
    "valid_from", "valid_to", "recorded_at", "last_seen_at",
    "confidence", "supersedes", "superseded_by", "source_session", "source_seq",
)

_SLUG_BAD = re.compile(r"[^\w\u4e00-\u9fff-]+")


def slugify(text: str, limit: int = 28) -> str:
    cleaned = _SLUG_BAD.sub("-", text.strip()).strip("-")
    return (cleaned or "memory")[:limit].strip("-") or "memory"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if re.fullmatch(r"[\w\u4e00-\u9fff .:/+@-]+", text) and not text.startswith(("-", "?")):
        return text
    return "'" + text.replace("'", "''") + "'"


def render_memory(mem: Memory, evidence: list[sqlite3.Row]) -> str:
    lines = ["---"]
    for key in FRONTMATTER_KEYS:
        lines.append(f"{key}: {_yaml_scalar(getattr(mem, key, None))}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {mem.content}")
    lines.append("")
    lines.append(f"- 对象：`{mem.object}`")
    lines.append(f"- 置信度：{mem.confidence:.2f}")
    valid = mem.valid_from or "?"
    valid_to = mem.valid_to or "至今"
    lines.append(f"- 有效区间：{valid} ~ {valid_to}")
    if mem.supersedes:
        lines.append(f"- 替代：`{mem.supersedes}`")
    if mem.superseded_by:
        lines.append(f"- 被替代：`{mem.superseded_by}`")
    if evidence:
        lines.append("")
        lines.append("## 证据")
        for row in evidence:
            quote = (row["quote"] or "").replace("\n", " ").strip()
            src = row["session_id"] or "?"
            seq = row["seq"] if row["seq"] is not None else "?"
            lines.append("")
            lines.append(f"> {quote}")
            lines.append(f"> — `{src}` · seq {seq} · {row['recorded_at']}")
    lines.append("")
    return "\n".join(lines)


def _rel_path(mem: Memory) -> str:
    return (
        f"{mem.kind}/{slugify(mem.subject)}-{slugify(mem.predicate)}-"
        f"{slugify(mem.object)}-{mem.id}.md"
    )


def render_index(mems: list[Memory]) -> str:
    order = {"active": 0, "candidate": 1, "superseded": 2, "rejected": 3}
    mems = sorted(mems, key=lambda m: (order.get(m.status, 9), m.kind, m.predicate))
    lines = [
        "# 记忆索引",
        "",
        f"共 {len(mems)} 条：active {sum(m.status == 'active' for m in mems)} / "
        f"candidate {sum(m.status == 'candidate' for m in mems)} / "
        f"superseded {sum(m.status == 'superseded' for m in mems)}",
        "",
        "| 状态 | kind | 谓词 | 值 | 置信度 | 最近见到 |",
        "|---|---|---|---|---|---|",
    ]
    for mem in mems:
        rel = _rel_path(mem)
        lines.append(
            f"| {mem.status} | {mem.kind} | `{mem.predicate}` | "
            f"[{mem.object}]({rel}) | {mem.confidence:.2f} | {mem.last_seen_at} |"
        )
    lines.append("")
    return "\n".join(lines)


def export(conn: sqlite3.Connection, outdir: str | Path) -> dict[str, int]:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    rows = conn.execute("SELECT * FROM memories ORDER BY kind, predicate, id").fetchall()
    mems = [row_to_memory(r) for r in rows]
    written = unchanged = 0
    expected: set[Path] = set()

    for mem in mems:
        evidence = conn.execute(
            "SELECT * FROM memory_evidence WHERE memory_id = ? ORDER BY id", (mem.id,)
        ).fetchall()
        target = out / _rel_path(mem)
        expected.add(target.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        content = render_memory(mem, evidence)
        if target.exists() and target.read_text(encoding="utf-8") == content:
            unchanged += 1
            continue
        target.write_text(content, encoding="utf-8")
        written += 1

    # 清理由本工具生成、但已不在当前记忆集里的旧文件（只删带 id: mem_ 的）
    removed = 0
    for stale in out.rglob("*.md"):
        if stale.name == "INDEX.md" or stale.resolve() in expected:
            continue
        try:
            head = stale.read_text(encoding="utf-8")[:200]
        except OSError:
            continue
        if "\nid: mem_" in f"\n{head}":
            stale.unlink(missing_ok=True)
            removed += 1

    index_path = out / "INDEX.md"
    index = render_index(mems)
    if not index_path.exists() or index_path.read_text(encoding="utf-8") != index:
        index_path.write_text(index, encoding="utf-8")
        written += 1
    else:
        unchanged += 1

    return {
        "total": len(mems),
        "written": written,
        "unchanged": unchanged,
        "removed": removed,
    }


def _default_outdir() -> Path:
    """默认导出目录：$DSH_HOME/dsh-tide-memory/memories/

    **绝不导到插件包内部**：导出的 Markdown 包含记忆全文和对话原文，
    放在安装目录既有泄漏风险（打包/提交时被带走），也会在升级时被清掉。
    """
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory" / "memories"


def main(argv: list[str] | None = None) -> int:
    import argparse

    from store import connect

    parser = argparse.ArgumentParser(description="导出记忆为 Markdown")
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--outdir",
        default=None,
        help="导出目录（默认 $DSH_HOME/dsh-tide-memory/memories/）",
    )
    args = parser.parse_args(argv)

    outdir = Path(args.outdir) if args.outdir else _default_outdir()
    conn = connect(args.db)
    stats = export(conn, outdir)
    print(
        f"导出完成：{stats['total']} 条，写入 {stats['written']} 个文件，"
        f"未变化 {stats['unchanged']} 个，清理 {stats['removed']} 个 -> "
        f"{outdir.resolve()}"
    )
    print("（提示：导出内容含记忆全文，请勿放进插件目录或提交到仓库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
