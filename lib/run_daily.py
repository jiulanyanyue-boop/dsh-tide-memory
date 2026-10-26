"""每日巩固任务：抽取 -> 巩固 -> 导出 ->（可选）git 提交。

用法
  python run_daily.py --db var/memory.db --outdir memories
  python run_daily.py --db var/memory.db --days 3 --git
  python run_daily.py --db var/memory.db --dry-run

设计
  - 只处理「修改时间已经稳定」的会话（默认 10 分钟前），避免啃正在写入的日志。
  - session_cursor 记录每个会话的 mtime，没变化就跳过，省 LLM 调用。
  - LLM 不可用时自动回退规则抽取，整晚不会跑空。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from consolidate import Outcome, build_decider, consolidate
from export import export
from extract_dsh import find_session_files, load_session
from extract_llm import extract, messages_from_events
from llm import LlmClient, load_llm_config
from store import Store, connect, utc_now

HERE = Path(__file__).resolve().parent


def _default_db() -> Path:
    """默认库：$DSH_HOME/dsh-tide-memory/memory.db（不写进插件包内部）。"""
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory" / "memory.db"


def _default_out() -> Path:
    """默认导出目录：$DSH_HOME/dsh-tide-memory/memories/

    **绝不导到插件包内部**：导出含记忆全文和对话原文，
    放在安装目录既有泄漏风险（打包/提交时被带走），也会在升级时被清掉。
    """
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory" / "memories"


DEFAULT_DB = _default_db()
DEFAULT_OUT = _default_out()


@dataclass
class DailyStats:
    sessions_seen: int = 0
    sessions_skipped: int = 0
    sessions_processed: int = 0
    candidates: int = 0
    actions: dict[str, int] = field(
        default_factory=lambda: {"insert": 0, "merge": 0, "invalidate": 0, "skip": 0}
    )
    llm_used: bool = False
    errors: list[str] = field(default_factory=list)

    def add(self, outcome: Outcome) -> None:
        self.actions[outcome.action] = self.actions.get(outcome.action, 0) + 1


def _cursor_mtime(store: Store, session_id: str) -> float | None:
    row = store.conn.execute(
        "SELECT source_mtime FROM session_cursor WHERE session_id = ?", (session_id,)
    ).fetchone()
    return float(row["source_mtime"]) if row else None


def _save_cursor(store: Store, session_id: str, mtime: float, count: int) -> None:
    store.conn.execute(
        "INSERT INTO session_cursor (session_id, source_mtime, last_run_at, candidates) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "source_mtime = excluded.source_mtime, "
        "last_run_at = excluded.last_run_at, "
        "candidates = excluded.candidates",
        (session_id, mtime, utc_now(), count),
    )
    store.conn.commit()


def _git_commit(outdir: Path, message: str) -> str:
    """outdir 在 git 仓库里时才提交；返回一句结果说明。"""
    try:
        inside = subprocess.run(
            ["git", "-C", str(outdir), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, check=False,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return "跳过 git：outdir 不在 git 仓库内"

        subprocess.run(
            ["git", "-C", str(outdir), "add", "-A"], check=True, capture_output=True
        )
        staged = subprocess.run(
            ["git", "-C", str(outdir), "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if not staged:
            return "git：没有变更"
        subprocess.run(
            ["git", "-C", str(outdir), "commit", "-m", message],
            check=True, capture_output=True,
        )
        return "git：已提交"
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        return f"git 失败：{exc}"


def run(
    *,
    db: str | Path = DEFAULT_DB,
    outdir: str | Path = DEFAULT_OUT,
    days: float = 1.0,
    min_age_minutes: float = 10.0,
    dry_run: bool = False,
    force: bool = False,
    git_commit: bool = False,
    limit_sessions: int | None = None,
    batch_size: int = 12,
    sessions_root: str | Path | None = None,
) -> DailyStats:
    stats = DailyStats()
    conn = connect(db)
    store = Store(conn)

    cfg = load_llm_config()
    client = LlmClient(cfg) if cfg else None
    stats.llm_used = client is not None

    cutoff = time.time() - days * 86400
    stable_cutoff = time.time() - min_age_minutes * 60

    root = Path(sessions_root) if sessions_root else None
    files = [
        p for p in find_session_files(root)
        if p.stat().st_mtime >= cutoff and p.stat().st_mtime <= stable_cutoff
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit_sessions:
        files = files[:limit_sessions]
    stats.sessions_seen = len(files)

    run_id = store.start_run()
    decider = build_decider()

    for path in files:
        mtime = path.stat().st_mtime
        try:
            session_id, events = load_session(path)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"{path.name}: 读取失败 {exc}")
            continue

        if not force and _cursor_mtime(store, session_id) == mtime:
            stats.sessions_skipped += 1
            continue

        messages = messages_from_events(events)
        if not messages:
            _save_cursor(store, session_id, mtime, 0)
            stats.sessions_skipped += 1
            continue

        try:
            report = extract(
                messages,
                session_id=session_id,
                client=client,
                batch_size=batch_size,
            )
        except Exception as exc:  # noqa: BLE001  网络类错误已在 client 里处理
            stats.errors.append(f"{session_id}: 抽取失败 {exc}")
            continue

        stats.candidates += len(report.candidates)
        if dry_run:
            for cand in report.candidates:
                print(f"  [dry-run] {cand.kind:<10} {cand.predicate:<18} {cand.object}")
            stats.sessions_processed += 1
            continue

        for cand in report.candidates:
            stats.add(consolidate(store, cand, decider))
        _save_cursor(store, session_id, mtime, len(report.candidates))
        stats.sessions_processed += 1

    store.finish_run({"candidates": stats.candidates, **stats.actions})

    # 消费 dsh-tide-memory 插件写下的 inbox（实时记录，延迟巩固）
    if not dry_run:
        try:
            from mem import flush_inbox

            inbox_stats = flush_inbox(use_llm=client is not None)
            if inbox_stats["entries"]:
                print(
                    f"inbox：{inbox_stats['entries']} 条输入 → "
                    f"{inbox_stats['candidates']} 条候选 "
                    f"(insert {inbox_stats['insert']} / merge {inbox_stats['merge']} / "
                    f"invalidate {inbox_stats['invalidate']} / skip {inbox_stats['skip']})"
                )
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"inbox 消费失败：{exc}")

    if not dry_run:
        exported = export(conn, outdir)
        try:
            from snapshot import snapshot_path_for, write_snapshot

            write_snapshot(conn, path=snapshot_path_for(db))
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"快照写入失败：{exc}")
        print(
            f"导出：{exported['total']} 条记忆，写入 {exported['written']}，"
            f"未变化 {exported['unchanged']}"
        )
        if git_commit:
            msg = f"memory: daily consolidation {utc_now()[:10]}"
            print(_git_commit(Path(outdir), msg))

    print(
        f"run {run_id}：会话 {stats.sessions_seen}，处理 {stats.sessions_processed}，"
        f"跳过 {stats.sessions_skipped}，候选 {stats.candidates}，"
        f"动作 {stats.actions}，LLM={'on' if stats.llm_used else 'off(规则)'}"
    )
    for err in stats.errors:
        print(f"  ! {err}", file=sys.stderr)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="每日记忆巩固")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--outdir", default=str(DEFAULT_OUT))
    parser.add_argument("--days", type=float, default=1.0, help="只处理最近 N 天修改的会话")
    parser.add_argument("--min-age-minutes", type=float, default=10.0,
                        help="跳过最近 N 分钟内还在写的会话")
    parser.add_argument("--dry-run", action="store_true", help="只抽取，不写库不导出")
    parser.add_argument("--force", action="store_true", help="忽略会话游标，全部重跑")
    parser.add_argument("--git", action="store_true", help="导出后提交 git（需 outdir 在仓库内）")
    parser.add_argument("--limit-sessions", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--sessions-root", default=None,
                        help="会话日志根目录（默认 ~/.dsh/sessions）")
    args = parser.parse_args(argv)

    run(
        db=args.db,
        outdir=args.outdir,
        days=args.days,
        min_age_minutes=args.min_age_minutes,
        dry_run=args.dry_run,
        force=args.force,
        git_commit=args.git,
        limit_sessions=args.limit_sessions,
        batch_size=args.batch_size,
        sessions_root=args.sessions_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
