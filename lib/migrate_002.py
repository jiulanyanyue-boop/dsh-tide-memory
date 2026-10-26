"""一次性迁移 002：给存量记忆补 scope / scope_ref / summary / depends_on / is_status。

用法：
    python migrate_002.py --dry-run              # 只看会怎么改
    python migrate_002.py                        # 真改（用当前目录当项目根）
    python migrate_002.py --project <项目路径>    # 指定项目根

设计：
  - 幂等，可反复跑（已填的不动）
  - 抽取失败的垃圾条目（predicate 不合法的那种）自动标 rejected，不物理删除
  - 不改任何 content/object，只补元数据
  - 不写死任何本机路径：项目根从 --project 或当前工作目录推导
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from store import Store, connect, utc_now  # noqa: E402
from memory_semantics import (  # noqa: E402
    ENV_PREDICATES,
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    SCOPE_WORKSPACE,
    STATUS_PREDICATES,
    USER_PREDICATES,
    Scope,
    find_project_root,
    make_summary,
    project_ref,
    workspace_ref,
)


def _default_db() -> Path:
    """默认库：$DSH_HOME/dsh-tide-memory/memory.db（与 mem.py 一致，不写死路径）。"""
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory" / "memory.db"


DEFAULT_DB = _default_db()

#: 已知归属：这些键一定是 global
FORCE_GLOBAL = set(USER_PREDICATES) | {
    "dsh_memento_maintenance",
    "dsh_memory_inject_mechanism",
    "dsh_memory_market_baseline",
    "dsh_plugin_registries",
    "memory_recall_long_text_penalty",
    "memory_needs_scope_and_lifecycle",
    "uses_dsh_plugin",
    "prefers_github_link_install",
}

#: DSH 生态知识：属 workspace
FORCE_WORKSPACE = set(ENV_PREDICATES) | {
    "plugin_react_usage",
    "plugin_client_entry_pattern",
    "host_switch_unusable",
    "settings_section_register_pattern",
    "client_service_sources",
}

#: 归档插件项目：属 project，绑定到 --project 指定的目录（默认当前目录）
FORCE_PROJECT = {
    "plugin_name",
    "plugin_features",
    "delete_bug_root_cause",
    "security_review_findings",
    "security_status",
    "requires_path_boundary_check",
    "fail_closed_on_activity_check",
    "build_status",
    "test_status",
    "deploy_status",
    "dev_phase",
    "publish_status",
    "open_issues",
    "current_blocker",
    "next_action",
}


def _is_junk_predicate(predicate: str) -> bool:
    """抽取失败的判据：谓词不是合法英文键（这就是那条垃圾记忆的特征）。

    不写死具体 id —— 换机器、换库都适用。
    """
    import re

    return not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", predicate or "")


def plan_for(predicate: str, subject: str, cwd: str, project_path: str) -> Scope:
    if predicate in FORCE_GLOBAL:
        return Scope(SCOPE_GLOBAL, "")
    if predicate in FORCE_WORKSPACE:
        return Scope(SCOPE_WORKSPACE, workspace_ref(cwd))
    if predicate in FORCE_PROJECT or predicate in STATUS_PREDICATES:
        return Scope(SCOPE_PROJECT, project_ref(project_path))
    if subject == "user":
        return Scope(SCOPE_GLOBAL, "")
    if subject == "project":
        return Scope(SCOPE_PROJECT, project_ref(project_path))
    return Scope(SCOPE_GLOBAL, "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MEMORY_DB", DEFAULT_DB))
    ap.add_argument("--cwd", default=os.getcwd(), help="工作区目录（判 workspace 归属）")
    ap.add_argument("--project", default=None,
                    help="项目根目录（判 project 归属；默认用 --cwd）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    project_path = args.project or args.cwd
    conn = connect(args.db)
    store = Store(conn)
    now = utc_now()

    rows = conn.execute(
        "SELECT id, kind, subject, predicate, object, content, scope, summary, "
        "depends_on, is_status FROM memories"
    ).fetchall()

    changed = {"scope": 0, "summary": 0, "depends_on": 0, "rejected": 0}
    for r in rows:
        mid = r["id"]
        updates: dict[str, object] = {}

        # 1. 抽取失败的条目标 rejected（判据：谓词不是合法英文键）
        if _is_junk_predicate(r["predicate"]):
            changed["rejected"] += 1
            if not args.dry_run:
                store.reject(mid, when=now, reason="manual")
                store.log(
                    action="reject",
                    memory_id=mid,
                    target_id=None,
                    candidate={"id": mid, "predicate": r["predicate"]},
                    decision={"action": "reject", "reason": "抽取失败：整句话被当成键"},
                    note="迁移 002：该条目谓词不合法（抽取失败产物），保留审计痕迹",
                )
            print(f"  [reject] {r['predicate']} = {r['object'][:40]}")
            continue

        # 2. 补 scope
        if (r["scope"] or "global") == SCOPE_GLOBAL and r["predicate"] in (
            FORCE_WORKSPACE | FORCE_PROJECT | STATUS_PREDICATES
        ):
            sc = plan_for(r["predicate"], r["subject"], args.cwd, project_path)
            updates["scope"] = sc.scope
            updates["scope_ref"] = sc.scope_ref
            changed["scope"] += 1

        # 3. 补 summary
        if not (r["summary"] or "").strip():
            sc = Scope(
                updates.get("scope", r["scope"] or SCOPE_GLOBAL),
                updates.get("scope_ref", ""),
            )
            updates["summary"] = make_summary(
                kind=r["kind"],
                predicate=r["predicate"],
                obj=r["object"],
                content=r["content"],
                scope=sc,
            )
            changed["summary"] += 1

        # 4. 补 depends_on（项目类）
        sc_final = updates.get("scope", r["scope"] or SCOPE_GLOBAL)
        if sc_final == SCOPE_PROJECT and not (r["depends_on"] or "").strip():
            root = find_project_root(project_path)
            updates["depends_on"] = f"path:{root}"
            changed["depends_on"] += 1

        # 5. 补 is_status
        updates["is_status"] = 1 if r["predicate"] in STATUS_PREDICATES else 0

        if updates and not args.dry_run:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE memories SET {sets}, updated_at = ? WHERE id = ?",
                (*updates.values(), now, mid),
            )
        print(
            f"  [{updates.get('scope', r['scope']):9}] {r['predicate']:38} "
            f"{(updates.get('summary') or r['summary'] or '')[:50]}"
        )

    if not args.dry_run:
        conn.commit()
        try:
            from snapshot import snapshot_path_for, write_snapshot

            write_snapshot(conn, path=snapshot_path_for(Path(args.db)))
        except Exception as exc:  # noqa: BLE001
            print(f"  （快照刷新失败，不影响迁移：{exc}）")

    tag = "预览" if args.dry_run else "完成"
    print(
        f"\n迁移 002 {tag}：补 scope {changed['scope']} · 补 summary {changed['summary']} · "
        f"补 depends_on {changed['depends_on']} · 标 rejected {changed['rejected']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
