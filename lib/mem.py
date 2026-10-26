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


def _candidates_from_text(text: str) -> list[Candidate]:
    """把一段自然语言解析成候选：优先 LLM，没配置或解析失败时退回规则。"""
    from llm import LlmClient, load_llm_config

    cfg = load_llm_config()
    if cfg is not None:
        try:
            from extract_llm import extract

            report = extract(
                [{"seq": None, "time": utc_now(), "text": text}],
                session_id="settings-ui",
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
    store.start_run()

    def respond(data: dict, code: int = 0) -> int:
        print(json.dumps(data, ensure_ascii=False))
        return code

    if op == "list":
        rows = conn.execute(
            "SELECT id, kind, subject, predicate, object, content, status, "
            "valid_from, valid_to, confidence, last_seen_at, "
            "scope, scope_ref, summary, depends_on, expired_by "
            "FROM memories ORDER BY "
            "CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, "
            "updated_at DESC"
        ).fetchall()
        return respond({"ok": True, "items": [dict(r) for r in rows]})

    if op == "maintain":
        stats = maintain_lifecycle(conn)
        return respond({"ok": True, "stats": stats})

    if op == "add_text":
        text = str(payload.get("text", "")).strip()
        if len(text) < 4:
            return respond({"ok": False, "error": "内容太短，至少 4 个字"})
        cands = _candidates_from_text(text)
        if not cands:
            return respond({
                "ok": False,
                "error": "没有从这段内容里解析出可保存的记忆（试试写得更具体）",
            })
        results = []
        for cand in cands:
            cand.source_session = "settings-ui"
            cand.quote = cand.quote or text[:300]
            outcome = consolidate(store, cand, RuleDecider())
            results.append({
                "action": outcome.action,
                "id": outcome.memory_id,
                "kind": cand.kind,
                "predicate": cand.predicate,
                "object": cand.object,
                "content": cand.content,
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

        cands = _candidates_from_text(text)
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
        outcome = consolidate(store, cand, build_decider(True))
        write_snapshot(conn, path=snapshot_path_for(db_path()))
        return respond({"ok": True, "action": outcome.action, "id": outcome.memory_id})

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
        return respond({
            "ok": True,
            "configured": cfg is not None,
            "base_url": cfg.base_url if cfg else "",
            "model": cfg.model if cfg else "",
            "api_key_masked": _mask_key(cfg.api_key) if cfg else "",
            "config_path": str(CONFIG_PATH),
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
        if payload.get("api_key"):
            current.api_key = str(payload["api_key"])
        path = save_llm_config(current)
        return respond({"ok": True, "path": str(path)})

    return respond({"ok": False, "error": f"未知 op: {op}"})


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
