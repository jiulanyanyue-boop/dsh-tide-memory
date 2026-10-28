"""记忆命令行：给「会话」用的极简入口。

  python mem.py context --query "用户这句话" [--limit 8]   # 调取相关记忆
  python mem.py remember --kind preference --predicate prefers_editor \
      --object neovim --content "用户偏好 neovim"            # 记录一条
  python mem.py ingest-user "我比较喜欢用 neovim"           # 从一句话抽取并记录
  python mem.py recent [--limit 10]                        # 最近记忆
  python mem.py stats

设计原则：默认不依赖 LLM（毫秒级、离线可用）；--llm 时才调用模型判定。
"""

from __future__ import annotations

import argparse
import json
import getpass
import os
import re
import sys
from pathlib import Path

# Windows 下管道 stdin 默认用 GBK，设置页/插件都按 UTF-8 传，这里统一
for stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from consolidate import (  # noqa: E402
    Candidate,
    LlmDecider,
    RuleDecider,
    consolidate,
)
from extract_dsh import extract_from_text  # noqa: E402
from memory_semantics import (  # noqa: E402
    kind_label,
    predicate_label,
    resolve_scope,
)
from search import search  # noqa: E402
from snapshot import snapshot_path_for, write_snapshot  # noqa: E402
from store import Store, connect, utc_now  # noqa: E402

def _default_db() -> Path:
    """默认记忆库位置：$DSH_HOME/dsh-tide-memory/memory.db；没有 DSH_HOME 就退回包的 data/。

    绝不写死本机绝对路径——换机、换用户名都能用。
    """
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory" / "memory.db"


DEFAULT_DB = _default_db()
KINDS = ("preference", "fact", "episode", "reflection")


def db_path() -> Path:
    return Path(os.environ.get("MEMORY_DB", DEFAULT_DB))


# --------------------------------------------------------------------------- #
# 通用设置（面板「设置」页用）：$DSH_HOME/dsh-tide-memory.json
#
# 为什么放这：host.js 的 loadConfig 本来就读这个文件（config 查找链的最后一环），
# 插件定时器每次跑都重新 resolveConfig，所以 Python 在这里改完，
# 下一次定时器触发就用上了——不用重启、不用通知 host。
# --------------------------------------------------------------------------- #

#: 允许从面板改的键与类型。白名单之外一律拒绝——
#: 配置文件里还有 db/inbox/python 这些路径项，绝不能从 HTTP 面板改。
USER_SETTINGS: dict[str, type] = {
    "autoConsolidate": bool,
    "dailyConsolidate": bool,
    "dailyConsolidateTime": str,
}

#: 每个键的人类说明，settings_get 原样带回，前端提示用
USER_SETTINGS_HINTS: dict[str, str] = {
    "autoConsolidate": "收件箱攒够后自动抽取并整理（无需手动命令或定时任务）",
    "dailyConsolidate": "每天固定时刻整理一次，并顺带做生命周期维护（依赖消失 / TTL）",
    "dailyConsolidateTime": "每日整理的时刻，24 小时制 HH:MM（例如 03:00）",
}

#: dailyConsolidateTime 的合法格式
_DAILY_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def user_settings_path() -> Path:
    """设置文件路径。**必须与 host.js loadConfig 的第三候选完全一致**：
    `$DSH_HOME/dsh-tide-memory.json`——否则 Python 写了 host 读不到，开关就是摆设。

    例外：测试环境（显式设置 MEMORY_DB）时跟着库走，避免污染真实配置。
    """
    if os.environ.get("MEMORY_DB"):
        return db_path().parent / "dsh-tide-memory.json"
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory.json"


def read_user_settings() -> dict:
    """读当前用户设置。文件不存在/坏掉就回空表——绝不抛异常。"""
    try:
        data = json.loads(user_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_user_settings(patch: dict) -> tuple[dict, list[str]]:
    """合并写入。只接受白名单内的键；非法值返回被拒绝的键列表。

    host.js 的 resolveConfig 会把这份文件与 DEFAULTS 合并，
    所以这里写什么，下一次巩固就按什么跑。
    """
    import re as _re

    current = read_user_settings()
    rejected: list[str] = []
    for key, value in patch.items():
        if key not in USER_SETTINGS:
            rejected.append(key)
            continue
        expected = USER_SETTINGS[key]
        if expected is bool:
            current[key] = value if isinstance(value, bool) else bool(value)
        elif expected is str:
            text = str(value).strip()
            if key == "dailyConsolidateTime" and not _DAILY_TIME_RE.match(text):
                # 时间格式不对就拒绝这一个键，而不是整包失败——
                # 用户可能同时改了开关和时间，开关不该被时间连坐
                rejected.append(key)
                continue
            current[key] = text
        else:
            current[key] = value
    try:
        target = user_settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise RuntimeError(f"设置写入失败：{exc}") from exc
    return current, rejected


def describe_user_settings() -> dict:
    """回给面板的形状：当前生效值（缺省用与 host.js DEFAULTS 相同的默认）。"""
    current = read_user_settings()
    return {
        "autoConsolidate": bool(current.get("autoConsolidate", True)),
        "dailyConsolidate": bool(current.get("dailyConsolidate", False)),
        "dailyConsolidateTime": str(current.get("dailyConsolidateTime", "03:00")),
    }


def build_decider(use_llm: bool):
    if not use_llm:
        return RuleDecider()
    from llm import load_llm_config

    cfg = load_llm_config()
    if cfg is None:
        return RuleDecider()
    return LlmDecider(cfg.base_url, cfg.model, cfg.api_key, timeout=cfg.timeout)


def _row_line(row) -> str:
    """给人看的一行：优先 summary，绝不露 predicate。"""
    summary = (row["summary"] if "summary" in row.keys() else "") or row["content"]
    scope = row["scope"] if "scope" in row.keys() else "global"
    ref = (row["scope_ref"] if "scope_ref" in row.keys() else "") or ""
    kind_cn = kind_label(row["kind"])
    where = ""
    if scope == "workspace":
        where = " · 本工作区"
    elif scope == "project":
        where = f" · 项目 {ref}"
    expired = row["expired_by"] if "expired_by" in row.keys() else None
    mark = f" ⚠已失效({expired})" if expired else ""
    return f"- [{kind_cn}]{where}{mark} {summary}"


# --------------------------------------------------------------------------- #
# 生命周期维护（迁移 002）
# --------------------------------------------------------------------------- #

def maintain_lifecycle(conn, *, when: str | None = None, dry_run: bool = False) -> dict[str, int]:
    """检查依赖与 TTL，让该失效的记忆失效。

    两条规则：
      1. depends_on 指的东西没了 -> scope-gone
      2. 超过 TTL 且最近没被支持 -> ttl

    返回 {reason: 条数}。dry_run=True 只统计不写。
    """
    from memory_semantics import DependsOn, dependency_alive, ttl_days_for

    when = when or utc_now()
    store = Store(conn)
    stats = {"scope-gone": 0, "ttl": 0}

    # 1. 依赖死亡
    for mem in store.with_dependencies():
        dep = DependsOn.parse(mem.depends_on)
        alive, why = dependency_alive(dep)
        if alive:
            continue
        stats["scope-gone"] += 1
        if not dry_run:
            store.expire(mem.id, when=when, reason="scope-gone")
            store.log(
                action="expire",
                memory_id=mem.id,
                target_id=None,
                candidate={"id": mem.id, "depends_on": mem.depends_on},
                decision={"action": "expire", "reason": why},
                note=f"依赖消失，自动失效：{why}",
            )

    # 2. TTL
    for row in conn.execute(
        "SELECT id, kind, scope, last_seen_at FROM memories "
        "WHERE status = 'active' AND (expired_by IS NULL)"
    ).fetchall():
        days = ttl_days_for(row["kind"], row["scope"])
        if not days:
            continue
        age = _age_days(row["last_seen_at"])
        if age <= days:
            continue
        stats["ttl"] += 1
        if not dry_run:
            store.expire(row["id"], when=when, reason="ttl")
            store.log(
                action="expire",
                memory_id=row["id"],
                target_id=None,
                candidate={"id": row["id"], "age_days": round(age, 1)},
                decision={"action": "expire", "ttl_days": days},
                note=f"超过 TTL {days} 天未再被支持（已 {age:.0f} 天）",
            )

    if not dry_run:
        try:
            write_snapshot(conn, path=snapshot_path_for(db_path()))
        except Exception:  # noqa: BLE001
            pass
    return stats


def _age_days(iso_ts: str) -> float:
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return 9999.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0)


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #

def cmd_context(args: argparse.Namespace) -> int:
    conn = connect(db_path())
    # 检索前先做一次廉价的生命周期维护（依赖消失/TTL）
    if not getattr(args, "no_maintain", False):
        try:
            maintain_lifecycle(conn)
        except Exception:  # noqa: BLE001
            pass
    hits = search(conn, args.query, limit=args.limit, cwd=getattr(args, "cwd", None) or None) if args.query else []
    if not hits:
        print("（记忆库无相关命中）")
        return 0

    print(f'<memory source="dsh-long-term" count="{len(hits)}">')
    print("以下是跨会话保留下来的长期记忆，仅作背景参考；与当前任务冲突时以用户最新说法为准。")
    for hit in hits:
        summary = hit.get("summary") or hit.get("content") or ""
        kind_cn = kind_label(hit.get("kind", ""))
        print(f"- [{kind_cn}] {summary}")
    print("</memory>")
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    conn = connect(db_path())
    rows = conn.execute(
        "SELECT id, kind, object, content, confidence, last_seen_at "
        "FROM memories WHERE status = 'active' AND valid_to IS NULL "
        "ORDER BY last_seen_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("（记忆库为空）")
        return 0
    for row in rows:
        print(_row_line(row))
    return 0


# --------------------------------------------------------------------------- #
# remember / ingest
# --------------------------------------------------------------------------- #

def _remember(cand: Candidate, use_llm: bool) -> None:
    conn = connect(db_path())
    store = Store(conn)
    store.start_run()
    outcome = consolidate(store, cand, build_decider(use_llm))
    # 刷新插件读的快照（失败不影响主流程）
    try:
        write_snapshot(conn, path=snapshot_path_for(db_path()))
    except Exception:  # noqa: BLE001
        pass
    print(f"{outcome.action}: {outcome.memory_id} {('— ' + outcome.reason) if outcome.reason else ''}")


def cmd_remember(args: argparse.Namespace) -> int:
    if args.kind not in KINDS:
        print(f"kind 必须是 {'/'.join(KINDS)}", file=sys.stderr)
        return 2
    cand = Candidate(
        kind=args.kind,
        subject=args.subject,
        predicate=args.predicate,
        object=args.object,
        content=args.content or args.object,
        confidence=args.confidence,
        quote=args.quote,
        source_session=args.session,
        source_seq=None,
        recorded_at=utc_now(),
        scope=getattr(args, "scope", "") or "",
        summary=getattr(args, "summary", "") or "",
        depends_on=getattr(args, "depends_on", "") or "",
        cwd=getattr(args, "cwd", "") or "",
        mentioned_path=getattr(args, "mentioned_path", "") or "",
    )
    _remember(cand, args.llm)
    return 0


def cmd_maintain(args: argparse.Namespace) -> int:
    conn = connect(db_path())
    stats = maintain_lifecycle(conn, dry_run=args.dry_run)
    tag = "（预览，未写库）" if args.dry_run else ""
    print(f"生命周期维护{tag}：")
    print(f"  依赖消失 scope-gone : {stats['scope-gone']}")
    print(f"  超期 ttl            : {stats['ttl']}")
    if sum(stats.values()) == 0:
        print("  （没有需要处理的记忆喵）")
    return 0


def cmd_by_scope(args: argparse.Namespace) -> int:
    conn = connect(db_path())
    store = Store(conn)
    rows = store.by_scope(args.which, args.ref, only_active=True)
    if not rows:
        print(f"（{args.which} 下没有有效记忆）")
        return 0
    label = {"global": "全局", "workspace": "工作区", "project": "项目"}[args.which]
    print(f"=== {label}（{len(rows)} 条）===")
    for m in rows:
        print(f"  · {m.summary or m.content}")
        if m.scope_ref:
            print(f"      归属：{m.scope_ref}")
    return 0


def cmd_ingest_user(args: argparse.Namespace) -> int:
    """从一句用户原话里抽候选并记录（规则版；LLM 见 extract_dsh --llm）。"""
    text = args.text.strip()
    if not text:
        print("空输入", file=sys.stderr)
        return 2
    got = extract_from_text(text)
    if not got:
        print("（没有识别到可长期保留的信息）")
        return 0
    for kind, predicate, obj in got:
        cand = Candidate(
            kind=kind,
            subject="user",
            predicate=predicate,
            object=obj,
            content=f"用户提到：{obj}",
            confidence=0.7,
            quote=text[:300],
            source_session=args.session or "cli",
            recorded_at=utc_now(),
        )
        _remember(cand, args.llm)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = connect(db_path())
    store = Store(conn)
    print(f"库：{db_path()}")
    print(f"总数 {store.count()} / active {store.count('active')} / "
          f"candidate {store.count('candidate')} / superseded {store.count('superseded')}")
    return 0


# --------------------------------------------------------------------------- #
# inbox：插件写、这里消费
# --------------------------------------------------------------------------- #

def inbox_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("MEMORY_INBOX", db_path().parent / "inbox.jsonl"))


def flush_inbox(*, use_llm: bool = False, path: str | None = None) -> dict[str, int]:
    """消费 dsh-tide-memory 插件写下的 inbox.jsonl，抽取并巩固，然后清空。

    幂等性靠巩固层的指纹去重；重复消费不会产生重复记忆。
    """
    target = inbox_path(path)
    stats = {"entries": 0, "candidates": 0, "insert": 0, "merge": 0,
             "invalidate": 0, "skip": 0}
    if not target.exists():
        return stats

    raw_lines = [ln for ln in target.read_text(encoding="utf-8").splitlines() if ln.strip()]
    entries: list[dict] = []
    for line in raw_lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            entries.append(item)
    stats["entries"] = len(entries)
    if not entries:
        target.write_text("", encoding="utf-8")
        return stats

    conn = connect(db_path())
    store = Store(conn)
    store.start_run()
    decider = build_decider(use_llm)

    from llm import load_llm_config
    from memory_semantics import resolve_scope_from_cwd

    def _place(cand, entry):
        """按 inbox 条目里的 cwd 补归属。这不是可选项：
        没有它，项目 A 说的话会被当成 global，漏进项目 B 的上下文。"""
        cwd = str(entry.get("cwd") or "")
        if not cand.scope:
            sc = resolve_scope_from_cwd(
                predicate=cand.predicate, subject=cand.subject, cwd=cwd
            )
            cand.scope, cand.scope_ref = sc.scope, sc.scope_ref
        cand.cwd = cwd
        # 顺手把这个工作区登记进索引：否则面板只有一串哈希，显示不出人话名字
        if cwd:
            try:
                import workspace_index

                workspace_index.register_workspace(conn, cwd=cwd)
            except Exception:  # noqa: BLE001
                pass
        return cand

    if use_llm and load_llm_config() is not None:
        from extract_llm import extract  # 延迟导入，避免无 LLM 时加载

        candidates = []
        for entry in entries:
            report = extract(
                [{"seq": entry.get("seq"), "time": entry.get("time"), "text": entry["text"],
                  "cwd": entry.get("cwd") or ""}],
                session_id=str(entry.get("session", "inbox")),
                cwd=str(entry.get("cwd") or ""),
            )
            for cand in report.candidates:
                candidates.append(_place(cand, entry))
    else:
        candidates = []
        for entry in entries:
            for kind, predicate, obj in extract_from_text(entry["text"]):
                cand = Candidate(
                    kind=kind,
                    subject="user",
                    predicate=predicate,
                    object=obj,
                    content=f"用户提到：{obj}",
                    confidence=0.6,
                    quote=entry["text"][:300],
                    source_session=str(entry.get("session", "inbox")),
                    source_seq=entry.get("seq"),
                    recorded_at=utc_now(),
                )
                candidates.append(_place(cand, entry))

    stats["candidates"] = len(candidates)
    for cand in candidates:
        outcome = consolidate(store, cand, decider)
        stats[outcome.action] = stats.get(outcome.action, 0) + 1

    store.finish_run({"inbox": stats["entries"], "candidates": stats["candidates"]})
    try:
        write_snapshot(conn, path=snapshot_path_for(db_path()))
    except Exception:  # noqa: BLE001
        pass
    target.write_text("", encoding="utf-8")
    return stats


def cmd_flush_inbox(args: argparse.Namespace) -> int:
    stats = flush_inbox(use_llm=args.llm, path=args.inbox)
    print(
        f"inbox：{stats['entries']} 条输入 → {stats['candidates']} 条候选 "
        f"(insert {stats['insert']} / merge {stats['merge']} / "
        f"invalidate {stats['invalidate']} / skip {stats['skip']})"
    )
    return 0


# --------------------------------------------------------------------------- #
# LLM 配置（DeepSeek 等 OpenAI 兼容端点）
# --------------------------------------------------------------------------- #

def _mask_key(key: str) -> str:
    if not key:
        return "(空)"
    if len(key) <= 10:
        return key[:2] + "…"
    return f"{key[:6]}…{key[-4:]}"


#: list 只回这些字段：前端渲染够用，且**不含** source_session / fingerprint 等内部信息
_LIST_FIELDS = (
    "id", "kind", "subject", "predicate", "object", "content", "status",
    "valid_from", "valid_to", "confidence", "last_seen_at",
    "scope", "scope_ref", "summary", "depends_on", "expired_by",
)

#: 这些 op 只读（或有自己的隔离库），不该在真实库留下任何写入。
#: 「测试生成」尤其重要：主人点它时必须保证零副作用。
READ_ONLY_OPS = frozenset({
    "list", "workspaces", "llm", "engine_status", "engine_test",
})


def _resolve_titles(conn, refs: set[str]) -> dict[str, str]:
    """批量把 scope_ref 翻成人话，避免每行都查一次注册表。

    三类 ref 各有各的翻法：
      ws:<hash>   → workspaces 表 → 注册表 title → 路径 basename
      path:<hash> → 反查真实路径 → basename（项目名）
      git:<slug>  → 仓库名
    """
    titles: dict[str, str] = {}
    for ref in refs:
        if not ref:
            continue
        try:
            import workspace_index

            title = ""
            if ref.startswith("ws:"):
                row = conn.execute(
                    "SELECT title, path FROM workspaces WHERE ref = ?", (ref,)
                ).fetchone()
                if row is not None:
                    title = row["title"] or ""
                    if not title and row["path"]:
                        title = Path(row["path"]).name
                if not title:
                    title = workspace_index.title_for_ref(ref)
            elif ref.startswith("path:"):
                # 项目身份：把哈希还原成真实路径，取末段当项目名。
                # 先查内存里的登记表（workspace_index 会扫描 DSH plugins 目录），
                # 再退到旧 scope-roots.json 的历史路径。
                try:
                    from memory_semantics import _load_scope_roots, _resolve_hashed_path

                    resolved = _resolve_hashed_path(ref)
                    if not resolved:
                        for root in _load_scope_roots():
                            if "path:" + __import__("hashlib").sha1(
                                root.encode("utf-8")
                            ).hexdigest()[:12] == ref:
                                resolved = root
                                break
                    if resolved:
                        title = Path(resolved).name
                except Exception:  # noqa: BLE001
                    title = ""
                if not title:
                    # 还原不出来时，别把哈希甩给用户看
                    title = "其它项目"
            elif ref.startswith("git:"):
                title = ref[len("git:"):].split("/")[-1]
            titles[ref] = title or ref
        except Exception:  # noqa: BLE001
            titles[ref] = ref
    return titles


def _is_visible(scope: str, scope_ref: str, visibility: dict) -> bool:
    """与 host.js / workspace_index 同口径的隔离判定（fail-closed）。"""
    scope = scope or "global"
    if scope == "global":
        return True
    if scope == "workspace":
        current = visibility.get("ref") or ""
        return bool(current) and scope_ref == current
    if scope == "project":
        return scope_ref in (visibility.get("project_refs") or [])
    return False


def _candidates_from_text(text: str, cwd: str = "") -> list[Candidate]:
    """把一段自然语言解析成候选：优先 LLM，没配置或解析失败时退回规则。"""
    from llm import LlmClient, load_llm_config

    cfg = load_llm_config()
    if cfg is not None:
        try:
            from extract_llm import extract

            report = extract(
                [{"seq": None, "time": utc_now(), "text": text, "cwd": cwd}],
                session_id="settings-ui",
                cwd=cwd,
                client=LlmClient(cfg),
            )
            if report.candidates:
                return report.candidates
        except Exception:  # noqa: BLE001  解析失败就走规则
            pass

    out: list[Candidate] = []
    for kind, predicate, obj in extract_from_text(text):
        out.append(
            Candidate(
                kind=kind,
                subject="user",
                predicate=predicate,
                object=obj,
                content=f"用户提到：{obj}",
                confidence=0.7,
                quote=text[:300],
                source_session="settings-ui",
                recorded_at=utc_now(),
            )
        )
    return out


def cmd_api(args: argparse.Namespace) -> int:
    """给设置页用的 JSON API：从 stdin 读一条请求，往 stdout 写一条结果。"""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "error": "invalid json"}, ensure_ascii=False))
        return 0

    op = payload.get("op")
    conn = connect(db_path())
    store = Store(conn)
    # 只读/测试类 op **不**在真实库开运行记录：否则点一次「测试生成」
    # 或刷新一次面板都会往 runs 表塞一行，日积月累把库撑脏。
    if op not in READ_ONLY_OPS:
        store.start_run()

    # ── 工作区上下文：所有读写都围着它转 ───────────────────────────────
    # cwd 缺失就 fail-closed：只放行 global（用户画像），绝不把别的
    # 工作区的记忆漏进当前视图。
    req_cwd = str(payload.get("cwd") or "").strip()
    req_ref = str(payload.get("workspace_ref") or "").strip()
    try:
        import workspace_index

        # 登记工作区只发生在**会写记忆**的 op 上。
        # 读类与测试类 op 绝不能顺手更新 last_seen_at —— 否则「点一次测试」
        # 也会在真实库留下痕迹，破坏零副作用承诺。
        if req_cwd and op not in READ_ONLY_OPS:
            workspace_index.register_workspace(conn, cwd=req_cwd)
        visibility = workspace_index.visible_refs(conn, cwd=req_cwd, ref=req_ref)
        current = workspace_index.current_workspace(conn, cwd=req_cwd, ref=req_ref)
    except Exception:  # noqa: BLE001  工作区索引挂了也不能让整个面板崩
        visibility = {"ref": "", "title": "", "path": "", "workspace_id": "",
                      "project_refs": [], "allow_global": True}
        current = {"ref": "", "path": "", "title": "", "workspace_id": ""}

    def respond(data: dict, code: int = 0) -> int:
        print(json.dumps(data, ensure_ascii=False))
        return code

    if op == "list":
        try:
            rows = conn.execute(
                f"SELECT {', '.join(_LIST_FIELDS)} FROM memories ORDER BY "
                "CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, "
                "updated_at DESC"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            return respond({"ok": False, "error": f"读取失败：{exc}"})

        # 隔离：只有当前工作区 + 用户画像（global）能出现在返回结果里。
        # 已失效（valid_to 非空）的条目单独走 archived 分组，不混进正常列表。
        visible_rows: list[dict] = []
        archived_rows: list[dict] = []
        hidden = 0
        for row in rows:
            item = dict(row)
            if not _is_visible(item.get("scope") or "global", item.get("scope_ref") or "", visibility):
                hidden += 1
                continue
            if item.get("valid_to") or item.get("expired_by"):
                archived_rows.append(item)
            else:
                visible_rows.append(item)

        refs = {item.get("scope_ref") or "" for item in visible_rows}
        refs |= {item.get("scope_ref") or "" for item in archived_rows}
        titles = _resolve_titles(conn, refs)
        for item in visible_rows:
            ref = item.get("scope_ref") or ""
            item["workspace_title"] = titles.get(ref, "") if ref else ""
        for item in archived_rows:
            ref = item.get("scope_ref") or ""
            item["workspace_title"] = titles.get(ref, "") if ref else ""

        # 归属分组：当前工作区的记忆 = workspace 命中当前 ref，**或** project
        # 命中当前 cwd 的项目身份。二者都算「本工作区的」，否则项目级记忆
        # 会被错误地塞进「其它工作区」里，看起来像串了。
        current_ref = visibility.get("ref") or ""
        project_refs = set(visibility.get("project_refs") or [])

        def belongs_here(item: dict) -> bool:
            scope = item.get("scope") or "global"
            ref = item.get("scope_ref") or ""
            if scope == "workspace":
                return bool(current_ref) and ref == current_ref
            if scope == "project":
                return ref in project_refs
            return False

        counts = {
            "current": sum(1 for i in visible_rows if belongs_here(i)),
            "global": sum(1 for i in visible_rows if (i.get("scope") or "global") == "global"),
            "other_workspaces": sum(
                1 for i in visible_rows
                if (i.get("scope") or "global") != "global" and not belongs_here(i)
            ),
            "hidden_other_workspaces": hidden,
        }

        # 跨工作区可见性说明：被挡掉的条目**不给内容**，只给条数分组，
        # 让用户知道「别处还有记忆」，但绝不会把内容漏过来。
        # 只统计「仍然有效」的条目——已失效的、被拒的不算「别处还有东西」。
        hidden_by_ref: dict[str, int] = {}
        for row in rows:
            item = dict(row)
            if _is_visible(item.get("scope") or "global", item.get("scope_ref") or "", visibility):
                continue
            if item.get("valid_to") or item.get("expired_by"):
                continue
            if (item.get("status") or "") in ("rejected", "superseded", "expired"):
                continue
            scope = item.get("scope") or "global"
            # global 的不会走到这里（global 永远可见），留个保险
            if scope == "global":
                continue
            ref = item.get("scope_ref") or ""
            hidden_by_ref[ref] = hidden_by_ref.get(ref, 0) + 1
        hidden_titles = _resolve_titles(conn, set(hidden_by_ref))
        hidden_groups = [
            {"scope_ref": ref, "title": hidden_titles.get(ref, ref or "未归属"), "count": n}
            for ref, n in sorted(hidden_by_ref.items(), key=lambda kv: -kv[1])
        ]
        counts["hidden_other_workspaces"] = sum(hidden_by_ref.values())
        return respond({
            "ok": True,
            "current": {
                "workspace_ref": current.get("ref") or "",
                "path": current.get("path") or "",
                "title": current.get("title") or "",
                "workspace_id": current.get("workspace_id") or "",
                "source": "workspace.json" if current.get("ref") else "none",
            },
            "items": visible_rows,
            "counts": counts,
            # 已失效/被取代的历史条目：仍可查（记忆永不物理删除），但单独一组
            "archived": archived_rows,
            # 只给「别处有多少条」，不给内容——这是隔离与可发现性的平衡点
            "hidden_groups": hidden_groups,
        })

    if op == "workspaces":
        # 只回元数据：ref / title / 条数。**绝不**回任何记忆内容，
        # 否则「切换工作区查看」就变成了跨工作区读取。
        try:
            import workspace_index

            items = workspace_index.list_workspaces(conn)
        except Exception as exc:  # noqa: BLE001
            return respond({"ok": False, "error": f"工作区索引不可用：{exc}"})
        return respond({
            "ok": True,
            "current_ref": current.get("ref") or "",
            "items": items,
        })

    if op == "register_workspace":
        try:
            import workspace_index

            result = workspace_index.register_workspace(
                conn,
                cwd=req_cwd,
                title=str(payload.get("title") or ""),
                workspace_id=str(payload.get("workspace_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            return respond({"ok": False, "error": f"登记失败：{exc}"})
        if not result.get("ref"):
            return respond({"ok": False, "error": "缺少可用的 cwd"})
        return respond({"ok": True, **result})

    if op == "maintain":
        stats = maintain_lifecycle(conn)
        return respond({"ok": True, "stats": stats})

    if op == "add_text":
        text = str(payload.get("text", "")).strip()
        if len(text) < 4:
            return respond({"ok": False, "error": "内容太短，至少 4 个字"})
        cands = _candidates_from_text(text, cwd=req_cwd)
        if not cands:
            return respond({
                "ok": False,
                "error": "没有从这段内容里解析出可保存的记忆（试试写得更具体）",
            })
        results = []
        for cand in cands:
            cand.source_session = "settings-ui"
            cand.quote = cand.quote or text[:300]
            cand.cwd = req_cwd
            # 与 op=add 保持同一个判定器：否则「新增记忆」两个入口的结论会打架
            outcome = consolidate(store, cand, build_decider(True))
            results.append({
                "action": outcome.action,
                "id": outcome.memory_id,
                "kind": cand.kind,
                "predicate": cand.predicate,
                "object": cand.object,
                "content": cand.content,
                "reason": getattr(outcome, "reason", ""),
            })
        write_snapshot(conn, path=snapshot_path_for(db_path()))
        return respond({"ok": True, "results": results})

    if op == "edit_text":
        memory_id = str(payload.get("id", "")).strip()
        text = str(payload.get("text", "")).strip()
        row = store.get(memory_id) if memory_id else None
        if row is None:
            return respond({"ok": False, "error": "记忆不存在"})
        if len(text) < 2:
            return respond({"ok": False, "error": "内容太短"})
        # 跨工作区编辑：当前工作区看不到的条目一律拒绝（fail-closed）
        if not _is_visible(row.scope or "global", row.scope_ref or "", visibility):
            return respond({"ok": False, "error": "这条记忆不属于当前工作区，已拒绝编辑"})

        cands = _candidates_from_text(text, cwd=req_cwd)
        fields: dict[str, object] = {"content": text}
        if cands:
            cand = cands[0]
            fields.update({
                "kind": cand.kind,
                "predicate": cand.predicate,
                "object": cand.object,
                "content": cand.content,
                "confidence": cand.confidence,
            })

        # 必须重算 summary：否则界面和注入会继续用旧内容。
        # （实测发现：改了内容但 summary 不变 → 面板看着没改、注入的也是旧句子）
        from memory_semantics import Scope, make_summary

        _scope = row["scope"] or "global"
        _ref = row["scope_ref"] or ""
        fields["summary"] = make_summary(
            kind=str(fields.get("kind", row["kind"])),
            predicate=str(fields.get("predicate", row["predicate"])),
            obj=str(fields.get("object", row["object"])),
            content=str(fields.get("content", row["content"])),
            scope=Scope(_scope, _ref),
        )

        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE memories SET {sets}, updated_at = ? WHERE id = ?",
            (*fields.values(), utc_now(), memory_id),
        )
        conn.commit()
        store.log(
            action="update", memory_id=memory_id, target_id=None,
            candidate={"source": "settings-ui", "text": text},
            decision={"fields": list(fields), "reparsed": bool(cands)},
            note="设置页编辑内容（参数由 LLM 重新判定）",
        )
        write_snapshot(conn, path=snapshot_path_for(db_path()))
        return respond({"ok": True, "reparsed": bool(cands)})

    if op == "add":
        obj = str(payload.get("object", "")).strip()
        predicate = str(payload.get("predicate", "")).strip().lower()
        kind = str(payload.get("kind", "fact")).strip()
        if kind not in KINDS or not predicate or len(obj) < 2:
            return respond({"ok": False, "error": "kind/predicate/object 不合法"})
        cand = Candidate(
            kind=kind,
            subject=str(payload.get("subject", "user")),
            predicate=predicate,
            object=obj,
            content=str(payload.get("content", "")).strip() or obj,
            confidence=float(payload.get("confidence", 0.8)),
            quote="（设置页手动添加）",
            source_session="settings-ui",
            recorded_at=utc_now(),
        )
        # 归属：手动添加也要跟当前工作区绑定，否则会一律落成 global
        cand.cwd = req_cwd
        if req_ref and not cand.scope:
            cand.scope, cand.scope_ref = "workspace", req_ref
        outcome = consolidate(store, cand, build_decider(True))
        write_snapshot(conn, path=snapshot_path_for(db_path()))
        return respond({
            "ok": True,
            "action": outcome.action,
            "id": outcome.memory_id,
            "reason": getattr(outcome, "reason", ""),
        })

    if op == "update":
        memory_id = str(payload.get("id", "")).strip()
        fields: dict[str, object] = {}
        for key in ("content", "object", "confidence", "status"):
            if key in payload:
                fields[key] = payload[key]
        if not memory_id or not fields:
            return respond({"ok": False, "error": "缺少 id 或字段"})
        if "status" in fields and fields["status"] not in (
            "active", "candidate", "superseded", "rejected"
        ):
            return respond({"ok": False, "error": "status 不合法"})
        if "confidence" in fields:
            fields["confidence"] = max(0.0, min(1.0, float(fields["confidence"])))
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE memories SET {sets}, updated_at = ? WHERE id = ?",
            (*fields.values(), utc_now(), memory_id),
        )
        conn.commit()
        store.log(
            action="update", memory_id=memory_id, target_id=None,
            candidate={"source": "settings-ui"},
            decision={"fields": fields}, note="设置页手动修改",
        )
        write_snapshot(conn, path=snapshot_path_for(db_path()))
        return respond({"ok": True})

    if op == "delete":
        memory_id = str(payload.get("id", "")).strip()
        row = store.get(memory_id) if memory_id else None
        if row is None:
            return respond({"ok": False, "error": "记忆不存在"})
        # 跨工作区删除同样 fail-closed
        if not _is_visible(row.scope or "global", row.scope_ref or "", visibility):
            return respond({"ok": False, "error": "这条记忆不属于当前工作区，已拒绝删除"})
        conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        conn.commit()
        store.log(
            action="delete", memory_id=memory_id, target_id=None,
            candidate={"source": "settings-ui", "content": row.content},
            decision={}, note="设置页手动删除",
        )
        write_snapshot(conn, path=snapshot_path_for(db_path()))
        return respond({"ok": True})

    if op == "llm":
        from llm import CONFIG_PATH, load_llm_config

        cfg = load_llm_config()
        # 只回掩码：明文密钥永远不出这个进程
        return respond({
            "ok": True,
            "configured": cfg is not None,
            "base_url": cfg.base_url if cfg else "",
            "model": cfg.model if cfg else "",
            "config_path": str(CONFIG_PATH),
            **_key_state(),
        })

    if op == "settings_get":
        return respond({
            "ok": True,
            "settings": describe_user_settings(),
            "hints": USER_SETTINGS_HINTS,
            "path": str(user_settings_path()),
        })

    if op == "settings_set":
        patch = payload.get("settings")
        if not isinstance(patch, dict) or not patch:
            return respond({"ok": False, "error": "缺少 settings 补丁"})
        try:
            saved, rejected = write_user_settings(patch)
        except RuntimeError as exc:
            return respond({"ok": False, "error": str(exc)})
        return respond({
            "ok": True,
            "settings": describe_user_settings(),
            # 告诉前端哪些键被拒绝（白名单外 / 格式非法），便于排查而不是静默吞掉
            "rejected": rejected,
            "path": str(user_settings_path()),
            "note": "host.js 的下一次巩固会读取新设置，立即生效，无需重启",
        })

    if op == "llm-clear":
        key_state = _clear_key()
        return respond({"ok": True, "config_path": str(_llm_config_path()), **key_state})

    if op == "engine_status":
        try:
            return respond(_engine_status())
        except Exception as exc:  # noqa: BLE001
            return respond({
                "ok": False,
                "error": f"引擎状态读取失败：{exc}",
                "engine": ENGINE_RULE,
                "engine_label": ENGINE_LABEL[ENGINE_RULE],
                "reason": "状态未知，按规则引擎处理",
                "checks": [],
            })

    if op == "engine_test":
        text = str(payload.get("text") or "").strip() or PROBE_TEXT
        # 长度上限：测试文本是给人看的探针，不是灌库入口
        if len(text) > 300:
            text = text[:300]
        try:
            return respond(_engine_test(text, req_cwd))
        except Exception as exc:  # noqa: BLE001
            return respond({
                "ok": False,
                "engine": ENGINE_RULE,
                "engine_label": ENGINE_LABEL[ENGINE_RULE],
                "degraded": True,
                "degrade_reason": f"{type(exc).__name__}: {exc}",
                "text": text,
                "candidates": [],
                "candidate_count": 0,
                "actions": {},
                "note": "测试未执行成功；未写入任何真实记忆",
            })

    if op == "llm-set":
        from llm import LlmConfig, load_llm_config, save_llm_config

        current = load_llm_config() or LlmConfig(
            base_url="https://api.deepseek.com/v1", model="deepseek-chat"
        )
        if payload.get("base_url"):
            current.base_url = str(payload["base_url"]).rstrip("/")
        if payload.get("model"):
            current.model = str(payload["model"])
        # 空串 = 不改密钥（前端「保存配置」不会误清密钥）
        submitted = str(payload.get("api_key") or "").strip()
        if submitted:
            current.api_key = submitted
        path = save_llm_config(current)
        # config_path 是 llm.json；密文路径在 secret_path / key.path。
        # 早期版本把两者都叫 path，前端分不清，这里明确区分。
        # configured 由**实际状态**判定，不顺口承诺「保存成功=已配置」：
        # 只改 base_url/model 而没密钥时，引擎其实仍是规则模式。
        st = _engine_status()
        return respond({
            "ok": True,
            "path": str(path),
            "config_path": str(path),
            "base_url": current.base_url,
            "model": current.model,
            "configured": st["llm"]["configured"],
            "engine": st["engine"],
            "engine_label": st["engine_label"],
            "engine_reason": st["reason"],
            **_key_state(),
        })

    return respond({"ok": False, "error": f"未知 op: {op}"})


# --------------------------------------------------------------------------- #
# 密钥状态（永不含明文）
# --------------------------------------------------------------------------- #

def _key_state() -> dict:
    """给面板看的密钥状态。**只含掩码与方案，绝不含明文。**

    ⚠️ v2：同时回**扁平**与**嵌套**两种形态。
    原因：前端按契约读嵌套 `key.scheme`，而早期实现只回扁平字段，
    前端读不到就把 scheme 退化成空串 → 徽标显示「未存储」，
    即使密钥其实已正确落盘。两种形态并存后，任一侧读法都能拿到真值。
    """
    try:
        import secret_store

        state = secret_store.describe()
        stored = bool(state.get("stored"))
        scheme = state.get("scheme") or "none"
        available = state.get("available_scheme") or "none"
        secret_path = state.get("path") or ""
        writable = bool(state.get("writable"))
        masked = state.get("api_key_masked") or ""
        decryptable = _can_decrypt(stored, scheme)
        inner = {
            "stored": stored,
            "scheme": scheme,
            "available_scheme": available,
            "path": secret_path,
            "writable": writable,
            "decryptable": decryptable,
        }
        return {
            "api_key_masked": masked,
            # 扁平形态（向后兼容旧前端读法）
            "stored": stored,
            "scheme": scheme,
            "available_scheme": available,
            "secret_path": secret_path,
            "writable": writable,
            "decryptable": decryptable,
            # 嵌套形态（契约 §3 v2，当前前端读法）
            "key": inner,
        }
    except Exception:  # noqa: BLE001
        empty = {
            "stored": False, "scheme": "none", "available_scheme": "none",
            "path": "", "writable": False, "decryptable": False,
        }
        return {
            "api_key_masked": "", "stored": False, "scheme": "none",
            "available_scheme": "none", "secret_path": "", "writable": False,
            "decryptable": False, "key": dict(empty),
        }


def _can_decrypt(stored: bool, scheme: str) -> bool:
    """密文能不能被当前用户解开。

    换机器/换用户后 DPAPI 会解不开，这时**不能**报「已加密可用」——
    面板要如实显示，否则主人会以为配好了、实际每次调用都失败。
    """
    if not stored or scheme == "none":
        return False
    try:
        import secret_store

        key, _scheme = secret_store.load_secret()
        return bool(key)
    except Exception:  # noqa: BLE001
        return False


def _clear_key() -> dict:
    try:
        import secret_store

        secret_store.clear_secret()
    except Exception:  # noqa: BLE001
        pass
    return _key_state()


def _llm_config_path() -> Path:
    """llm.json 的位置（与 secret 文件区分开，别再让 path 打架）。"""
    from llm import CONFIG_PATH

    return Path(CONFIG_PATH)


# --------------------------------------------------------------------------- #
# 引擎状态：现在到底走 LLM 还是规则？
#
# 判定必须 fail-closed：只有「配置齐全 + 密钥真能解开」才敢报 LLM。
# 否则主人以为在跑模型，实际每次都在走正则兜底——那才是最难查的坑。
# --------------------------------------------------------------------------- #

ENGINE_LLM = "llm"
ENGINE_RULE = "rule"

ENGINE_LABEL = {
    ENGINE_LLM: "LLM 生成模式",
    ENGINE_RULE: "规则引擎模式",
}


def _engine_status() -> dict:
    """返回当前**实际会生效**的引擎。只读，不触网、不调模型。"""
    from llm import load_llm_config

    cfg = load_llm_config()
    key = _key_state()
    checks: list[dict] = []

    configured = cfg is not None
    checks.append({
        "name": "LLM 配置",
        "ok": configured,
        "detail": (f"{cfg.base_url} · {cfg.model}" if configured
                   else "还没填 base_url / model"),
    })

    stored = bool(key.get("stored"))
    decryptable = bool(key.get("decryptable"))
    checks.append({
        "name": "API Key",
        "ok": stored and decryptable,
        "detail": ("密文可解密" if (stored and decryptable)
                   else ("密文存在但解不开（可能换了机器/用户）" if stored
                         else "还没保存密钥")),
    })
    checks.append({
        "name": "加密方案",
        "ok": bool(key.get("scheme")) and key.get("scheme") != "none",
        "detail": str(key.get("scheme") or "none"),
    })

    if not configured:
        engine, reason = ENGINE_RULE, "未配置 LLM（缺 base_url 或 model），走规则引擎"
    elif not stored:
        engine, reason = ENGINE_RULE, "已配置 LLM 但没保存 API Key，走规则引擎"
    elif not decryptable:
        engine, reason = ENGINE_RULE, "API Key 密文无法解密，走规则引擎（请重新保存密钥）"
    else:
        engine, reason = ENGINE_LLM, f"已配置 {cfg.model}，密钥可解密"

    return {
        "ok": True,
        "engine": engine,
        "engine_label": ENGINE_LABEL[engine],
        "reason": reason,
        "llm": {
            "configured": configured,
            "base_url": cfg.base_url if cfg else "",
            "model": cfg.model if cfg else "",
            "key": {
                "stored": stored,
                "scheme": key.get("scheme") or "none",
                "decryptable": decryptable,
            },
        },
        "checks": checks,
    }


#: 测试生成用的探针句：必须是**明确可抽取的结论句**，
#: 否则「抽出 0 条」时无法区分「引擎正常但内容不值得记」和「引擎挂了」。
PROBE_TEXT = "这个项目用 SQLite 存长期记忆，零第三方依赖，不引入任何外部库"


def _engine_test(text: str, cwd: str) -> dict:
    """跑一次**完整**的记忆生成流程，但什么都不留下。

    做法：在临时目录里新建一个库，走与真实流程完全相同的
    extract → consolidate 路径，跑完把临时目录整个删掉。
    真实库一个字节都不动，所以主人点多少次都没有副作用。

    `fallback=False` 是关键：extract 默认会在 LLM 失败时静默降级到规则，
    那样引擎判定就成了谎报。这里必须让失败**显式冒出来**。
    """
    import shutil
    import tempfile
    import time as _time

    from llm import LlmClient, load_llm_config

    status = _engine_status()
    cfg = load_llm_config()
    started = _time.time()

    result = {
        "ok": True,
        "engine": status["engine"],
        "engine_label": status["engine_label"],
        "reason": status["reason"],
        "degraded": False,
        "degrade_reason": None,
        "text": text,
        "candidates": [],
        "candidate_count": 0,
        "actions": {},
        "llm": status["llm"],
        "note": "本测试在临时数据库中执行完整抽取+巩固流程，未写入任何真实记忆",
    }

    tmp_root = tempfile.mkdtemp(prefix="tide-engine-test-")
    try:
        probe_db = Path(tmp_root) / "memory.db"
        probe_conn = connect(probe_db)
        probe_store = Store(probe_conn)
        probe_store.start_run()

        candidates: list[Candidate] = []
        if status["engine"] == ENGINE_LLM and cfg is not None:
            try:
                from extract_llm import extract

                report = extract(
                    [{"seq": None, "time": utc_now(), "text": text, "cwd": cwd}],
                    session_id="engine-test",
                    cwd=cwd,
                    client=LlmClient(cfg),
                    fallback=False,
                )
                candidates = list(report.candidates)
            except Exception as exc:  # noqa: BLE001
                # LLM 真的失败了：如实上报降级，而不是假装成功
                result["engine"] = ENGINE_RULE
                result["engine_label"] = ENGINE_LABEL[ENGINE_RULE]
                result["degraded"] = True
                result["degrade_reason"] = f"{type(exc).__name__}: {exc}"

        if status["engine"] != ENGINE_LLM or result["degraded"]:
            # 规则引擎路径（也是真实运行时会走的同一条路）
            for kind, predicate, obj in extract_from_text(text):
                candidates.append(Candidate(
                    kind=kind, subject="user", predicate=predicate, object=obj,
                    content=f"用户提到：{obj}", confidence=0.6,
                    quote=text[:300], source_session="engine-test",
                    recorded_at=utc_now(), cwd=cwd,
                ))

        for cand in candidates:
            cand.cwd = cand.cwd or cwd
            outcome = consolidate(probe_store, cand, build_decider(status["engine"] == ENGINE_LLM))
            action = getattr(outcome, "action", "skip")
            result["actions"][action] = result["actions"].get(action, 0) + 1

        result["candidates"] = [
            {
                "kind": c.kind,
                "predicate": c.predicate,
                "object": c.object,
                "content": c.content[:200],
                "confidence": float(c.confidence or 0),
            }
            for c in candidates
        ]
        result["candidate_count"] = len(result["candidates"])
        probe_conn.close()
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["degraded"] = True
        result["degrade_reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        # 临时库连同里面可能写入的东西一起删掉——这是「零副作用」的保证
        shutil.rmtree(tmp_root, ignore_errors=True)

    result["elapsed_ms"] = int((_time.time() - started) * 1000)
    return result


def cmd_llm(args: argparse.Namespace) -> int:
    from llm import (
        CONFIG_PATH,
        LlmClient,
        LlmConfig,
        LlmError,
        load_llm_config,
        save_llm_config,
    )

    if args.action == "show":
        cfg = load_llm_config()
        if cfg is None:
            print("尚未配置 LLM。")
            print(f"配置文件路径：{CONFIG_PATH}")
            print("填法：mem.cmd llm set --base-url https://api.deepseek.com/v1 "
                  "--model deepseek-chat（随后会安全提示输入密钥）")
            return 0
        source = "环境变量 TIDE_MEM_LLM_*" if LlmConfig.from_env() else str(CONFIG_PATH)
        print(f"base_url : {cfg.base_url}")
        print(f"model    : {cfg.model}")
        print(f"api_key  : {_mask_key(cfg.api_key)}")
        print(f"来源     : {source}")
        return 0

    if args.action == "set":
        cfg = load_llm_config() or LlmConfig(
            base_url="https://api.deepseek.com/v1", model="deepseek-chat"
        )
        if args.base_url:
            cfg.base_url = args.base_url.rstrip("/")
        if args.model:
            cfg.model = args.model
        if args.api_key is not None:
            print("错误：--api-key 已移除，避免密钥出现在命令历史和进程参数中。", file=sys.stderr)
            print("请交互输入密钥，或通过 --api-key-stdin 从标准输入读取。", file=sys.stderr)
            return 2
        if args.api_key_stdin:
            cfg.api_key = sys.stdin.readline().rstrip("\r\n")
            if not cfg.api_key:
                print("错误：标准输入中没有 API key。", file=sys.stderr)
                return 2
        else:
            if not sys.stdin.isatty():
                print("当前输入不是交互终端；自动化环境请使用 --api-key-stdin。", file=sys.stderr)
                return 2
            try:
                cfg.api_key = getpass.getpass("API key（输入不回显，留空保留现有值）：") or cfg.api_key
            except (EOFError, OSError):
                print("无法安全交互读取 API key；自动化环境请使用 --api-key-stdin。", file=sys.stderr)
                return 2
        path = save_llm_config(cfg)
        print(f"已保存 -> {path}")
        print(f"base_url : {cfg.base_url}")
        print(f"model    : {cfg.model}")
        print(f"api_key  : {_mask_key(cfg.api_key)}")
        print("可以用 `mem.cmd llm test` 验证连通性。")
        return 0

    if args.action == "test":
        cfg = load_llm_config()
        if cfg is None:
            print("还没配置：mem.cmd llm set（随后会安全提示输入密钥）", file=sys.stderr)
            return 1
        client = LlmClient(cfg)
        try:
            out = client.chat_json(
                "你是一个连通性检查端点。只输出 JSON，不要解释。",
                {"instruction": '输出 {"ok": true, "model": "<你的模型名>"}'},
            )
            print(f"连接成功（{cfg.model}）：{json.dumps(out, ensure_ascii=False)}")
            return 0
        except LlmError as exc:
            print(f"连接失败：{exc}", file=sys.stderr)
            return 1

    return 2



# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长期记忆命令行")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ctx = sub.add_parser("context", help="按查询调取记忆（模型可读）")
    p_ctx.add_argument("--query", default="")
    p_ctx.add_argument("--limit", type=int, default=8)
    p_ctx.add_argument("--cwd", default=None,
                       help="按此工作目录过滤归属（不给则只返回全局记忆）")
    p_ctx.add_argument("--no-maintain", action="store_true",
                       help="跳过检索前的生命周期维护")
    p_ctx.set_defaults(func=cmd_context)

    p_recent = sub.add_parser("recent", help="最近记忆")
    p_recent.add_argument("--limit", type=int, default=10)

    p_rem = sub.add_parser("remember", help="记录一条记忆")
    p_rem.add_argument("--kind", required=True)
    p_rem.add_argument("--subject", default="user")
    p_rem.add_argument("--predicate", required=True)
    p_rem.add_argument("--object", required=True)
    p_rem.add_argument("--content", default="")
    p_rem.add_argument("--quote", default="")
    p_rem.add_argument("--session", default="cli")
    p_rem.add_argument("--confidence", type=float, default=0.7)
    p_rem.add_argument("--llm", action="store_true", help="用 LLM 判定（需 TIDE_MEM_LLM_*）")
    # 迁移 002：归属与生命周期
    p_rem.add_argument("--cwd", default="", help="工作目录（判归属用）")
    p_rem.add_argument("--path", dest="mentioned_path", default="",
                       help="提到的项目路径（判项目归属 + 绑定依赖）")
    p_rem.add_argument("--scope", default="", choices=["", "global", "workspace", "project"])
    p_rem.add_argument("--summary", default="", help="给主人看的一句话（不填自动生成）")
    p_rem.add_argument("--depends-on", default="", help="path:<绝对路径> / git-remote:<url>")

    p_ing = sub.add_parser("ingest-user", help="从一句用户原话抽取并记录")
    p_ing.add_argument("text")
    p_ing.add_argument("--session", default="cli")
    p_ing.add_argument("--cwd", default="")
    p_ing.add_argument("--llm", action="store_true")

    sub.add_parser("stats", help="统计")

    p_maint = sub.add_parser("maintain", help="生命周期维护（依赖消失 / TTL 过期）")
    p_maint.add_argument("--dry-run", action="store_true", help="只统计不写库")

    p_scope = sub.add_parser("by-scope", help="按归属列出记忆")
    p_scope.add_argument("which", choices=["global", "workspace", "project"])
    p_scope.add_argument("--ref", default=None)

    p_flush = sub.add_parser("flush-inbox", help="消费插件写的 inbox.jsonl")
    p_flush.add_argument("--inbox", default=None, help="inbox 路径（默认 var/inbox.jsonl）")
    p_flush.add_argument("--llm", action="store_true")

    p_llm = sub.add_parser("llm", help="LLM 配置（DeepSeek 等 OpenAI 兼容端点）")
    p_llm.add_argument("action", choices=["show", "set", "test"])
    p_llm.add_argument("--base-url", default=None)
    p_llm.add_argument("--model", default=None)
    p_llm.add_argument("--api-key", default=None, help=argparse.SUPPRESS)
    p_llm.add_argument("--api-key-stdin", action="store_true",
                        help="从标准输入读取 API key（适用于自动化；不要把 key 放进命令行）")

    sub.add_parser("api", help="给设置页用的 JSON API（stdin -> stdout）")

    args = parser.parse_args(argv)
    handlers = {
        "context": cmd_context,
        "recent": cmd_recent,
        "remember": cmd_remember,
        "ingest-user": cmd_ingest_user,
        "stats": cmd_stats,
        "maintain": cmd_maintain,
        "by-scope": cmd_by_scope,
        "flush-inbox": cmd_flush_inbox,
        "llm": cmd_llm,
        "api": cmd_api,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
