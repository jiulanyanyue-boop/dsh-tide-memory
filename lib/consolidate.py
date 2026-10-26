"""巩固状态机：把「候选记忆」合并进记忆库。

核心思想
  模型只做语义判断（duplicate / complement / conflict / novel），
  状态迁移、失效标记、来源回填、审计日志全部由代码兜住。
  没有配置 LLM 时退化为规则判定，依然可用。

用法
  python consolidate.py --db var/memory.db init
  python consolidate.py --db var/memory.db demo
  python consolidate.py --db var/memory.db add --kind preference \
      --subject user --predicate prefers_editor --object vim --content "用户偏好 vim"
  python consolidate.py --db var/memory.db search "编辑器"
  python consolidate.py --db var/memory.db stats
  python consolidate.py --db var/memory.db audit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from store import Memory, Store, connect, ms_to_iso, utc_now
from llm import open_llm_request, validate_base_url
from memory_semantics import (
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    STATUS_PREDICATES,
    Scope,
    find_project_root,
    make_summary,
    resolve_scope,
    worth_remembering,
)

try:  # Windows 控制台默认 cp936，强制 UTF-8 不那么容易炸
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
PROMPT_PATH = HERE / "prompts" / "consolidate.md"


def _default_db() -> Path:
    """默认库：$DSH_HOME/dsh-tide-memory/memory.db。

    **不写进插件包内部**——插件目录升级/重装会被替换，还可能只读。
    """
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory" / "memory.db"


DEFAULT_DB = _default_db()

Action = Literal["insert", "merge", "invalidate", "skip"]  # 判定器输出
OutcomeAction = Literal["insert", "merge", "invalidate", "skip", "reject"]  # 最终动作


# --------------------------------------------------------------------------- #
# 候选与决策
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    kind: str
    subject: str
    predicate: str
    object: str
    content: str
    confidence: float = 0.6
    quote: str = ""
    source_session: str | None = None
    source_seq: int | None = None
    recorded_at: str = field(default_factory=utc_now)
    # 迁移 002：归属与语义
    scope: str = ""
    scope_ref: str = ""
    summary: str = ""
    depends_on: str = ""
    cwd: str = ""            # 说话时的工作目录（判归属用）
    mentioned_path: str = ""  # 提到的具体路径（判项目归属用）

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "content": self.content,
            "confidence": self.confidence,
            "quote": self.quote,
            "source_session": self.source_session,
            "source_seq": self.source_seq,
            "recorded_at": self.recorded_at,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "summary": self.summary,
            "depends_on": self.depends_on,
        }


@dataclass
class Decision:
    action: Action
    target_id: str | None = None
    reason: str = ""
    confidence: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target_id": self.target_id,
            "reason": self.reason,
            "confidence": self.confidence,
        }


# --------------------------------------------------------------------------- #
# 归一化与相似度
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[，。！？；：、,.!?;:\"'“”‘’（）()\[\]【】]")


def normalize(text: str) -> str:
    """归一化：折叠空白、去标点、英文小写。中文不做额外处理。"""
    text = _PUNCT.sub("", text or "")
    text = _WS.sub("", text)
    return text.strip().lower()


def fingerprint(kind: str, subject: str, predicate: str, obj: str) -> str:
    raw = "|".join([kind, subject, predicate, normalize(obj)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _tokens(text: str) -> set[str]:
    """中文按 2-gram，英文按词。够用且不求人。"""
    norm = normalize(text)
    if not norm:
        return set()
    if re.search(r"[\u4e00-\u9fff]", norm):
        return {norm[i : i + 2] for i in range(max(1, len(norm) - 1))}
    return set(re.split(r"[^a-z0-9]+", norm)) - {""}


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_refinement(old: str, new: str) -> bool:
    """old 是否只是 new 的「细化版」（可以合并，而不是冲突）。

    英文短词容易误判：'vim' 是 'neovim' 的子串但语义不同，
    所以含拉丁字母时要求词边界；中文没有词边界，直接允许子串细化。
    """
    o, n = normalize(old), normalize(new)
    if len(o) < 2 or o not in n or o == n:
        return False
    if re.search(r"[a-z0-9]", o):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(o) + r"(?![a-z0-9])", n))
    return True


# --------------------------------------------------------------------------- #
# 判定器
# --------------------------------------------------------------------------- #

class RuleDecider:
    """不依赖模型的兜底判定：查重 -> 补充 -> 冲突。"""

    name = "rule"

    def __init__(self, merge_threshold: float = 0.55) -> None:
        self.merge_threshold = merge_threshold

    def decide(self, cand: Candidate, neighbors: list[Memory]) -> Decision:
        if not neighbors:
            return Decision("insert", reason="没有同 key 的有效记忆")

        fp = fingerprint(cand.kind, cand.subject, cand.predicate, cand.object)
        same = [m for m in neighbors if m.fingerprint == fp]
        if same:
            return Decision(
                "skip",
                target_id=same[0].id,
                reason="指纹相同，已存在同一条记忆",
                confidence=0.95,
            )

        best = max(neighbors, key=lambda m: similarity(m.object, cand.object))
        sim = similarity(best.object, cand.object)

        # 同一个 key、旧值只是新值的细化（"中文" -> "中文简体"）：合并
        if sim >= self.merge_threshold or is_refinement(best.object, cand.object):
            return Decision(
                "merge",
                target_id=best.id,
                reason=f"语义相近或为细化（sim={sim:.2f}），并入已有条目",
                confidence=max(sim, 0.6),
            )

        # 同一个 key、值明显不同：冲突，旧条目失效
        return Decision(
            "invalidate",
            target_id=best.id,
            reason=f"同 key 不同值（sim={sim:.2f}），旧事实失效",
            confidence=1.0 - sim,
        )


class LlmDecider:
    """让模型只做语义判定；返回不可用结果时自动回退到规则。"""

    name = "llm"

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = 45.0, fallback: RuleDecider | None = None) -> None:
        self.base_url = validate_base_url(base_url)
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.fallback = fallback or RuleDecider()

    def decide(self, cand: Candidate, neighbors: list[Memory]) -> Decision:
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "candidate": cand.to_json(),
                            "neighbors": [
                                {
                                    "id": m.id,
                                    "object": m.object,
                                    "content": m.content,
                                    "valid_from": m.valid_from,
                                    "confidence": m.confidence,
                                }
                                for m in neighbors
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with open_llm_request(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            data = json.loads(content)
            action = data.get("action")
            if action not in ("insert", "merge", "invalidate", "skip"):
                raise ValueError(f"非法 action: {action!r}")
            target = data.get("target_id")
            if action in ("merge", "skip", "invalidate") and not target:
                raise ValueError("缺少 target_id")
            if target and not any(m.id == target for m in neighbors):
                raise ValueError("target_id 不在邻居集合里")
            return Decision(
                action,
                target_id=target,
                reason=str(data.get("reason", ""))[:500],
                confidence=float(data.get("confidence", 0.7)),
            )
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError,
                json.JSONDecodeError, OSError) as exc:
            decision = self.fallback.decide(cand, neighbors)
            decision.reason = f"[LLM 不可用，回退规则] {decision.reason} ({exc})"
            return decision


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    action: OutcomeAction
    memory_id: str
    target_id: str | None = None
    reason: str = ""


def _new_id() -> str:
    return "mem_" + hashlib.sha1(os.urandom(16)).hexdigest()[:12]


def _memory_from(cand: Candidate, when: str, *, supersedes: str | None = None,
                 status: str = "active") -> Memory:
    now = when
    return Memory(
        id=_new_id(),
        kind=cand.kind,
        subject=cand.subject,
        predicate=cand.predicate,
        object=cand.object.strip(),
        content=cand.content.strip(),
        status=status,
        valid_from=cand.recorded_at or now,
        valid_to=None,
        recorded_at=cand.recorded_at or now,
        last_seen_at=now,
        confidence=max(0.0, min(1.0, cand.confidence)),
        supersedes=supersedes,
        superseded_by=None,
        source_session=cand.source_session,
        source_seq=cand.source_seq,
        fingerprint=fingerprint(cand.kind, cand.subject, cand.predicate, cand.object),
        created_at=now,
        updated_at=now,
        scope=cand.scope or SCOPE_GLOBAL,
        scope_ref=cand.scope_ref or "",
        summary=cand.summary or "",
        depends_on=cand.depends_on or "",
        expired_by=None,
        is_status=1 if cand.predicate in STATUS_PREDICATES else 0,
    )


def enrich_candidate(cand: Candidate) -> tuple[Candidate, Any]:
    """补全归属、可读性、依赖。返回 (候选, GateResult)。

    这是「写入前最后一次把话说清楚」的地方：
      - 过闸门（不值得记的直接否决）
      - 定 scope / scope_ref
      - 生成 summary
      - 推断 depends_on
    """
    # 1. 闸门
    gate = worth_remembering(
        kind=cand.kind,
        predicate=cand.predicate,
        obj=cand.object,
        content=cand.content,
        quote=cand.quote,
    )
    if not gate.ok:
        return cand, gate

    # 2. 归属（已有值就不覆盖，允许调用方显式指定）
    scope = Scope(cand.scope or "", cand.scope_ref or "")
    if not scope.scope:
        scope = resolve_scope(
            predicate=cand.predicate,
            subject=cand.subject,
            cwd=cand.cwd,
            mentioned_path=cand.mentioned_path,
        )
        cand.scope, cand.scope_ref = scope.scope, scope.scope_ref

    # 3. 摘要
    if not cand.summary:
        cand.summary = make_summary(
            kind=cand.kind,
            predicate=cand.predicate,
            obj=cand.object,
            content=cand.content,
            scope=scope,
        )

    # 4. 依赖：项目类且知道路径 -> 绑定到那个路径
    if not cand.depends_on and scope.scope == SCOPE_PROJECT and cand.mentioned_path:
        root = find_project_root(cand.mentioned_path)
        cand.depends_on = f"path:{root}"

    return cand, gate


MIN_ACTIVE_CONFIDENCE = 0.5


def consolidate(
    store: Store,
    cand: Candidate,
    decider: Any,
    *,
    when: str | None = None,
    min_active_confidence: float = MIN_ACTIVE_CONFIDENCE,
    apply_gate: bool = True,
) -> Outcome:
    """把一条候选并进记忆库，返回最终动作。

    置信度低于 min_active_confidence 的新记忆进候选池（status='candidate'），
    之后被更高置信度的证据支持时才晋升为 active。

    迁移 002：进库前先过三问闸门 + 补归属/摘要/依赖（apply_gate=True 时）。
    闸门不过 -> 记一条 rejected 审计，但不进库。
    """
    when = when or utc_now()

    if apply_gate:
        cand, gate = enrich_candidate(cand)
        if not gate.ok:
            store.log(
                action="reject",
                memory_id=None,
                target_id=None,
                candidate=cand.to_json(),
                decision={"action": "reject", "reason": gate.reason},
                note=f"闸门否决（{gate.code}）：{gate.reason}",
            )
            return Outcome("reject", "", reason=f"闸门否决（{gate.code}）：{gate.reason}")

    neighbors = store.neighbors(
        kind=cand.kind,
        subject=cand.subject,
        predicate=cand.predicate,
        # 归属隔离：只在本归属内找邻居。
        # 否则项目 B 的「数据库用 MySQL」会 invalidate 掉项目 A 的「用 PostgreSQL」。
        scope=cand.scope or "global",
        scope_ref=cand.scope_ref or "",
    )
    decision = decider.decide(cand, neighbors)
    target = store.get(decision.target_id) if decision.target_id else None

    if decision.action == "skip" and decision.target_id:
        store.touch(decision.target_id, when, confidence=cand.confidence)
        note = "重复记忆，仅刷新 last_seen"
        if target and target.status == "candidate" and cand.confidence >= min_active_confidence:
            store.promote(decision.target_id, when=when, confidence=cand.confidence)
            note += "；证据变强，从候选池晋升"
        store.add_evidence(
            decision.target_id,
            session_id=cand.source_session,
            seq=cand.source_seq,
            quote=cand.quote or cand.content,
            recorded_at=when,
        )
        store.log(
            action="skip",
            memory_id=decision.target_id,
            target_id=None,
            candidate=cand.to_json(),
            decision=decision.to_json(),
            note=note,
        )
        outcome = Outcome("skip", decision.target_id, reason=decision.reason)

    elif decision.action == "merge" and decision.target_id:
        store.merge(
            decision.target_id,
            object=cand.object.strip(),
            content=cand.content.strip(),
            confidence=0.1,
            when=when,
        )
        note = "补充合并"
        if target and target.status == "candidate" and cand.confidence >= min_active_confidence:
            store.promote(decision.target_id, when=when, confidence=cand.confidence)
            note += "；证据变强，从候选池晋升"
        store.add_evidence(
            decision.target_id,
            session_id=cand.source_session,
            seq=cand.source_seq,
            quote=cand.quote or cand.content,
            recorded_at=when,
        )
        store.log(
            action="merge",
            memory_id=decision.target_id,
            target_id=None,
            candidate=cand.to_json(),
            decision=decision.to_json(),
            note=note,
        )
        outcome = Outcome("merge", decision.target_id, reason=decision.reason)

    elif decision.action == "invalidate" and decision.target_id:
        # 旧条目还在候选池里：它从来不是「事实」，直接覆盖，不必走失效链
        if target and target.status == "candidate":
            store.merge(
                decision.target_id,
                object=cand.object.strip(),
                content=cand.content.strip(),
                confidence=0.1,
                when=when,
            )
            if cand.confidence >= min_active_confidence:
                store.promote(decision.target_id, when=when, confidence=cand.confidence)
            store.add_evidence(
                decision.target_id,
                session_id=cand.source_session,
                seq=cand.source_seq,
                quote=cand.quote or cand.content,
                recorded_at=when,
            )
            store.log(
                action="merge",
                memory_id=decision.target_id,
                target_id=None,
                candidate=cand.to_json(),
                decision=decision.to_json(),
                note="覆盖候选池中的低置信度条目",
            )
            return Outcome("merge", decision.target_id, reason="覆盖候选池条目")

        new_mem = _memory_from(
            cand,
            when,
            supersedes=decision.target_id,
            status="active" if cand.confidence >= min_active_confidence else "candidate",
        )
        store.insert(new_mem)
        store.invalidate(decision.target_id, when=when, new_id=new_mem.id)
        store.add_evidence(
            new_mem.id,
            session_id=cand.source_session,
            seq=cand.source_seq,
            quote=cand.quote or cand.content,
            recorded_at=when,
        )
        store.log(
            action="invalidate",
            memory_id=new_mem.id,
            target_id=decision.target_id,
            candidate=cand.to_json(),
            decision=decision.to_json(),
            note="旧事实标失效，写入新事实",
        )
        outcome = Outcome("invalidate", new_mem.id, decision.target_id, decision.reason)

    else:  # insert（或 LLM 给了非法组合，兜底）
        mem = _memory_from(
            cand,
            when,
            status="active" if cand.confidence >= min_active_confidence else "candidate",
        )
        store.insert(mem)
        store.add_evidence(
            mem.id,
            session_id=cand.source_session,
            seq=cand.source_seq,
            quote=cand.quote or cand.content,
            recorded_at=when,
        )
        store.log(
            action="insert",
            memory_id=mem.id,
            target_id=None,
            candidate=cand.to_json(),
            decision=decision.to_json(),
            note="新记忆" if mem.status == "active" else "置信度不足，进候选池",
        )
        outcome = Outcome("insert", mem.id, reason=decision.reason)

    return outcome


# --------------------------------------------------------------------------- #
# 演示
# --------------------------------------------------------------------------- #

DEMO_STORY: list[tuple[str, str, str, str, str]] = [
    # (kind, subject, predicate, object, content)
    ("preference", "user", "prefers_editor", "vim", "用户偏好用 vim 写代码"),
    ("preference", "user", "prefers_editor", "vim", "用户说过喜欢 vim"),
    ("preference", "user", "prefers_editor", "neovim", "用户改用 neovim 了"),
    ("preference", "user", "responds_in", "中文", "用户希望回复用中文"),
    ("preference", "user", "responds_in", "中文简体", "用户希望回复用简体中文"),
    ("fact", "project", "db_engine", "sqlite", "这个项目用 SQLite 做记忆库"),
    ("fact", "project", "db_engine", "postgres", "后来换成 Postgres 了"),
]


def run_demo(db_path: str | Path) -> None:
    conn = connect(db_path)
    store = Store(conn)
    store.start_run()
    decider = build_decider()
    print(f"判定器：{getattr(decider, 'name', type(decider).__name__)}")
    print("-" * 72)

    for kind, subject, predicate, obj, content in DEMO_STORY:
        cand = Candidate(
            kind=kind,
            subject=subject,
            predicate=predicate,
            object=obj,
            content=content,
            confidence=0.7,
            quote=f"（演示）{content}",
            source_session="demo-session",
            source_seq=len(store.audit()) + 1,
        )
        out = consolidate(store, cand, decider)
        print(f"[{out.action:>10}] {content}")
        print(f"              -> {out.reason}")

    store.finish_run({})
    print("-" * 72)
    print(f"总条数 {store.count()}，有效 {store.count('active')}，失效 {store.count('superseded')}")
    print("\n有效记忆：")
    for row in conn.execute(
        "SELECT kind, predicate, object, confidence FROM memories "
        "WHERE status='active' ORDER BY kind, predicate"
    ):
        print(f"  {row['kind']:<10} {row['predicate']:<16} {row['object']:<20} "
              f"conf={row['confidence']:.2f}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_decider() -> Any:
    from llm import load_llm_config

    cfg = load_llm_config()
    if cfg is not None:
        return LlmDecider(cfg.base_url, cfg.model, cfg.api_key, timeout=cfg.timeout)
    return RuleDecider()


def _print_rows(rows: list[Any]) -> None:
    if not rows:
        print("（空）")
        return
    for row in rows:
        print("  ".join(f"{k}={row[k]}" for k in row.keys()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记忆巩固器（草稿实现）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库")

    p_add = sub.add_parser("add", help="手动写入一条候选")
    p_add.add_argument("--kind", required=True,
                       choices=["preference", "fact", "episode", "reflection"])
    p_add.add_argument("--subject", default="user")
    p_add.add_argument("--predicate", required=True)
    p_add.add_argument("--object", required=True)
    p_add.add_argument("--content", default="")
    p_add.add_argument("--quote", default="")
    p_add.add_argument("--confidence", type=float, default=0.7)

    sub.add_parser("demo", help="跑内置故事")

    p_search = sub.add_parser("search", help="检索有效记忆")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)

    sub.add_parser("stats", help="统计")
    sub.add_parser("audit", help="最近的审计记录")

    p_list = sub.add_parser("list", help="列出记忆")
    p_list.add_argument("--status", default="active")

    p_ingest = sub.add_parser("ingest", help="从候选 JSON 文件导入并巩固")
    p_ingest.add_argument("file", help="Candidate 数组的 JSON 文件，'-' 表示 stdin")

    p_sess = sub.add_parser("ingest-session", help="从 DSH 会话抽取并巩固")
    p_sess.add_argument("--session", help="会话 id 或日志路径")
    p_sess.add_argument("--latest", action="store_true", help="取最近修改的会话")
    p_sess.add_argument("--limit", type=int, default=30)
    p_sess.add_argument("--dry-run", action="store_true", help="只打印候选，不写库")

    p_export = sub.add_parser("export", help="把记忆导出为 Markdown")
    p_export.add_argument(
        "--outdir",
        default=None,
        help="导出目录（默认 $DSH_HOME/dsh-tide-memory/memories/，不写进插件包）",
    )

    args = parser.parse_args(argv)
    conn = connect(args.db)
    store = Store(conn)

    if args.cmd == "init":
        print(f"已初始化：{Path(args.db).resolve()}")
        print(f"schema 版本：{conn.execute('PRAGMA user_version').fetchone()[0]}")
        return 0

    if args.cmd == "demo":
        run_demo(args.db)
        return 0

    if args.cmd == "add":
        store.start_run()
        cand = Candidate(
            kind=args.kind,
            subject=args.subject,
            predicate=args.predicate,
            object=args.object,
            content=args.content or args.object,
            confidence=args.confidence,
            quote=args.quote,
            source_session="cli",
            source_seq=None,
        )
        out = consolidate(store, cand, build_decider())
        print(f"{out.action}: {out.memory_id} ({out.reason})")
        return 0

    if args.cmd == "search":
        from search import search

        results = search(conn, args.query, limit=args.limit)
        if not results:
            print("没有命中。")
            return 0
        for r in results:
            print(f"[{r['score']:.3f}] {r['content']}  "
                  f"({r['kind']}/{r['predicate']}={r['object']}, "
                  f"updated {r['last_seen_at']})")
        return 0

    if args.cmd == "stats":
        print(f"总数     {store.count()}")
        print(f"有效     {store.count('active')}")
        print(f"失效     {store.count('superseded')}")
        print(f"候选     {store.count('candidate')}")
        for row in conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 5"):
            print(f"run {row['run_id']} started={row['started_at']} "
                  f"finished={row['finished_at']} stats={row['stats_json']}")
        return 0

    if args.cmd == "audit":
        for row in store.audit():
            print(f"#{row['id']} {row['ts']} {row['action']:<10} "
                  f"mem={row['memory_id']} target={row['target_id']} note={row['note']}")
        return 0

    if args.cmd == "ingest":
        raw = (
            sys.stdin.read()
            if args.file == "-"
            else Path(args.file).read_text(encoding="utf-8")
        )
        payload = json.loads(raw)
        if isinstance(payload, dict):
            payload = payload.get("candidates", [])
        store.start_run()
        decider = build_decider()
        stats = {"insert": 0, "merge": 0, "invalidate": 0, "skip": 0}
        for item in payload:
            cand = Candidate(
                kind=item.get("kind", "fact"),
                subject=item.get("subject", "user"),
                predicate=item.get("predicate", "stated"),
                object=item["object"],
                content=item.get("content") or item["object"],
                confidence=float(item.get("confidence", 0.6)),
                quote=item.get("quote", ""),
                source_session=item.get("source_session"),
                source_seq=item.get("source_seq"),
                recorded_at=item.get("recorded_at") or utc_now(),
            )
            out = consolidate(store, cand, decider)
            stats[out.action] = stats.get(out.action, 0) + 1
            print(f"{out.action:>10}  {cand.content}")
            print(f"            {out.reason}")
        store.finish_run(stats)
        print(f"summary: {stats}")
        return 0

    if args.cmd == "ingest-session":
        from extract_dsh import candidates_from_events, find_session_files, load_session

        if args.session and Path(args.session).exists():
            path = Path(args.session)
        elif args.session:
            matches = [
                p for p in find_session_files()
                if args.session in str(p).replace("\\", "/")
            ]
            if not matches:
                print(f"没找到会话：{args.session}", file=sys.stderr)
                return 1
            path = matches[0]
        elif args.latest:
            files = find_session_files()
            if not files:
                print("没有找到任何会话日志。", file=sys.stderr)
                return 1
            path = files[0]
        else:
            print("请指定 --session 或 --latest", file=sys.stderr)
            return 2

        session_id, events = load_session(path)
        cands = candidates_from_events(events, session_id)[: args.limit]
        print(f"会话 {session_id}：{len(events)} 个事件，{len(cands)} 条候选")
        if args.dry_run:
            for c in cands:
                print(f"  [{c.kind}] {c.predicate} = {c.object}")
            return 0

        store.start_run()
        decider = build_decider()
        stats = {"insert": 0, "merge": 0, "invalidate": 0, "skip": 0}
        for cand in cands:
            out = consolidate(store, cand, decider)
            stats[out.action] = stats.get(out.action, 0) + 1
            print(f"{out.action:>10}  {cand.content}")
        store.finish_run(stats)
        print(f"summary: {stats}")
        return 0

    if args.cmd == "export":
        from export import _default_outdir, export

        outdir = args.outdir if args.outdir else str(_default_outdir())
        stats = export(conn, outdir)
        print(
            f"导出完成：{stats['total']} 条，写入 {stats['written']} 个文件，"
            f"未变化 {stats['unchanged']} 个，清理 {stats['removed']} 个 -> "
            f"{Path(outdir).resolve()}"
        )
        print("（提示：导出内容含记忆全文，请勿放进插件目录或提交到仓库）")
        return 0

    if args.cmd == "list":
        rows = conn.execute(
            "SELECT id, kind, subject, predicate, object, status, valid_to, "
            "confidence FROM memories WHERE status = ? ORDER BY updated_at DESC",
            (args.status,),
        ).fetchall()
        _print_rows(rows)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
