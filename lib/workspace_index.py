"""工作区登记表：把「工作区」变成可索引、可展示、可隔离的一等公民。

为什么需要它
------------
旧实现里 workspace 归属只是一个 sha1 哈希（`ws:<12 hex>`），没地方反解成人话，
面板只能打出「本工作区」这种模糊标签；不同工作区之间也没有独立的可查询索引。
本模块负责三件事：

  ① 登记表 `workspaces`：ref / path / title / workspace_id，`workspaces` 表落库；
  ② 展示名解析：ref → 「default-workspace」这种人话（DSH 注册表优先，路径 basename 兜底）；
  ③ 隔离判定：给出「当前工作区可见的 scope 集合」，任何查询都必须过这道闸。

权威来源优先级（title）
-----------------------
  1. `$DSH_HOME/storages/workspace.json` 里 title（用户改过的名字，最可信）
  2. 路径 basename
  3. 哈希本身（最后兜底，绝不再拿它当主显示）

注意：**绝不**把工作区路径写进任何日志以外的持久文件——路径本身在 workspaces 表里，
只用于反查标题；查询隔离只依赖 ref，不依赖路径字符串比较。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from memory_semantics import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    SCOPE_WORKSPACE,
    normalize_path,
    workspace_ref,
)


# --------------------------------------------------------------------------- #
# DSH 工作区注册表
# --------------------------------------------------------------------------- #

def _dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def _registry_path() -> Path:
    return _dsh_home() / "storages" / "workspace.json"


def load_registry() -> list[dict[str, str]]:
    """读 DSH 自己的工作区注册表，转成 [{ref, path, title, workspace_id}]。

    读不到就返回空列表——设置页只是少一个更好看的名字，不该因此报错。
    """
    target = _registry_path()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(document, dict):
        return []
    tables = document.get("tables")
    if not isinstance(tables, dict):
        return []
    rows = tables.get("workspaces")
    if not isinstance(rows, dict):
        return []
    global_meta = document.get("global") if isinstance(document.get("global"), dict) else {}
    order = global_meta.get("workspaceIds")
    order = order if isinstance(order, list) else list(rows.keys())

    out: list[dict[str, str]] = []
    for workspace_id in order:
        row = rows.get(workspace_id)
        if not isinstance(row, dict):
            continue
        path = row.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        title = row.get("title")
        out.append({
            "ref": workspace_ref(path),
            "path": normalize_path(path),
            "title": title.strip() if isinstance(title, str) and title.strip() else "",
            "workspace_id": str(workspace_id),
            "_raw_path": path,
        })
    return out


def title_for_ref(ref: str) -> str:
    """ref → 人话。注册表里没有就退回哈希前的兜底（调用方应再试库里登记的标题）。"""
    if not ref:
        return ""
    for row in load_registry():
        if row["ref"] == ref and row["title"]:
            return row["title"]
    return ""


def workspace_id_for_ref(ref: str) -> str:
    if not ref:
        return ""
    for row in load_registry():
        if row["ref"] == ref:
            return row["workspace_id"]
    return ""


def ref_for_cwd(cwd: str) -> str:
    """会话 cwd → 工作区 ref。与 memory_semantics.workspace_ref 同一算法。"""
    if not cwd:
        return ""
    return workspace_ref(cwd)


def cwd_for_ref(ref: str) -> str:
    """反查：ref → 归一化路径（注册表优先）。"""
    for row in load_registry():
        if row["ref"] == ref:
            return row["path"]
    return ""


# --------------------------------------------------------------------------- #
# 表结构 + 登记
# --------------------------------------------------------------------------- #

WORKSPACES_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
  ref          TEXT PRIMARY KEY,
  path         TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL DEFAULT '',
  workspace_id TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT '',
  last_seen_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_workspaces_seen ON workspaces(last_seen_at);
"""


def ensure_workspaces_table(conn) -> None:
    """幂等建表。老库跑一次即可，不需要迁移脚本。"""
    try:
        conn.executescript(WORKSPACES_DDL)
        conn.commit()
    except Exception:  # noqa: BLE001
        # 建表失败不该让整个记忆功能挂掉；调用方会退化成「没有工作区索引」
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_workspace(
    conn,
    *,
    cwd: str,
    title: str = "",
    workspace_id: str = "",
) -> dict[str, str]:
    """登记（或刷新）一个工作区。返回 {ref, path, title, workspace_id}。

    幂等：同 ref 反复调用只更新 title / workspace_id / last_seen_at。
    `title` 空时依次尝试 注册表 → 路径 basename。
    """
    if not cwd:
        return {"ref": "", "path": "", "title": "", "workspace_id": ""}
    path = normalize_path(cwd)
    ref = workspace_ref(cwd)
    if not ref:
        return {"ref": "", "path": path, "title": "", "workspace_id": ""}

    resolved_title = (title or "").strip()
    resolved_id = (workspace_id or "").strip()
    if not resolved_title:
        resolved_title = title_for_ref(ref)
    if not resolved_id:
        resolved_id = workspace_id_for_ref(ref)
    if not resolved_title:
        resolved_title = Path(path).name or path

    try:
        ensure_workspaces_table(conn)
        now = _now()
        conn.execute(
            """
            INSERT INTO workspaces (ref, path, title, workspace_id, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ref) DO UPDATE SET
              path = excluded.path,
              title = CASE WHEN excluded.title <> '' THEN excluded.title ELSE workspaces.title END,
              workspace_id = CASE WHEN excluded.workspace_id <> ''
                                  THEN excluded.workspace_id ELSE workspaces.workspace_id END,
              last_seen_at = excluded.last_seen_at
            """,
            (ref, path, resolved_title, resolved_id, now, now),
        )
        conn.commit()
    except Exception:  # noqa: BLE001
        pass
    return {"ref": ref, "path": path, "title": resolved_title, "workspace_id": resolved_id}


def current_workspace(conn, cwd: str = "", ref: str = "") -> dict[str, str]:
    """算出「当前工作区」三元信息。**纯读，一个字节都不写库。**

    早期实现会顺手 register（更新 last_seen_at），这让「读一下列表」和
    「点一次测试生成」都变成写操作。为了让 engine_test 真正做到零副作用，
    写库的职责只留给显式调用方（cmd_api 在写类 op 里才会 register）。
    """
    resolved_ref = (ref or "").strip()
    if not resolved_ref and cwd:
        resolved_ref = workspace_ref(cwd)
    if not resolved_ref:
        return {"ref": "", "path": "", "title": "", "workspace_id": ""}

    path = normalize_path(cwd) if cwd else cwd_for_ref(resolved_ref)
    title = ""
    workspace_id = ""

    try:
        row = conn.execute(
            "SELECT ref, title, path, workspace_id FROM workspaces WHERE ref = ?",
            (resolved_ref,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        row = None
    if row is not None:
        title = (row["title"] or "").strip()
        path = (row["path"] or "").strip() or path
        workspace_id = (row["workspace_id"] or "").strip()

    if not title:
        title = title_for_ref(resolved_ref)
    if not workspace_id:
        workspace_id = workspace_id_for_ref(resolved_ref)
    if not title and path:
        title = Path(path).name

    return {
        "ref": resolved_ref,
        "path": path,
        "title": title,
        "workspace_id": workspace_id,
    }


def list_workspaces(conn) -> list[dict[str, object]]:
    """所有已登记工作区 + 记忆条数。只回元数据，**不含任何记忆内容**。"""
    try:
        ensure_workspaces_table(conn)
        rows = conn.execute(
            """
            SELECT w.ref, w.path, w.title, w.workspace_id, w.last_seen_at,
                   (SELECT COUNT(*) FROM memories m
                     WHERE m.scope_ref = w.ref AND m.status IN ('active', 'candidate')
                       AND m.valid_to IS NULL) AS active_count,
                   (SELECT COUNT(*) FROM memories m
                     WHERE m.scope_ref = w.ref) AS total_count
              FROM workspaces w
             ORDER BY w.last_seen_at DESC
            """
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []

    out: list[dict[str, object]] = []
    for row in rows:
        title = row["title"] or title_for_ref(row["ref"]) or Path(row["path"]).name or row["ref"]
        out.append({
            "ref": row["ref"],
            "path": row["path"],
            "title": title,
            "workspace_id": row["workspace_id"] or workspace_id_for_ref(row["ref"]),
            "last_seen_at": row["last_seen_at"],
            "active": int(row["active_count"] or 0),
            "total": int(row["total_count"] or 0),
        })
    # 补上「注册表里有、但从未说过话」的工作区（条数 0），让面板能完整展示
    known = {item["ref"] for item in out}
    for row in load_registry():
        if row["ref"] in known:
            continue
        out.append({
            "ref": row["ref"],
            "path": row["path"],
            "title": row["title"] or Path(row["path"]).name,
            "workspace_id": row["workspace_id"],
            "last_seen_at": "",
            "active": 0,
            "total": 0,
        })
    return out


# --------------------------------------------------------------------------- #
# 隔离闸门
# --------------------------------------------------------------------------- #

def visible_refs(conn, *, cwd: str = "", ref: str = "") -> dict[str, object]:
    """算出「当前会话能看见哪些记忆」。

    返回 {ref, title, path, workspace_id, project_refs, allow_global}

    规则（与 host.js 的 isVisibleInSession 同口径，fail-closed）：
      · global   → 永远可见（用户画像跨项目通用）
      · workspace → 仅当 scope_ref == 当前 ref
      · project   → 仅当 scope_ref 属于当前 cwd 的项目身份集合
      · 其余未知 scope → 不可见

    拿不到当前工作区时 project_refs 为空、ref 为空 —— 于是只有 global 能被看见。
    """
    current = current_workspace(conn, cwd=cwd, ref=ref)
    project_refs: list[str] = []
    if cwd:
        try:
            from memory_semantics import known_project_refs

            project_refs = known_project_refs(cwd)
        except Exception:  # noqa: BLE001
            project_refs = []
    return {
        "ref": current["ref"],
        "title": current["title"],
        "path": current["path"],
        "workspace_id": current["workspace_id"],
        "project_refs": project_refs,
        "allow_global": True,
    }


def is_visible(item_scope: str, item_ref: str, visibility: dict[str, object]) -> bool:
    scope = item_scope or SCOPE_GLOBAL
    if scope == SCOPE_GLOBAL:
        return True
    if scope == SCOPE_WORKSPACE:
        current_ref = visibility.get("ref") or ""
        return bool(current_ref) and item_ref == current_ref
    if scope == SCOPE_PROJECT:
        return item_ref in (visibility.get("project_refs") or [])
    return False
