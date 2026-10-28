"""从 DSH 会话日志里抽候选记忆。

数据源
  ~/.dsh/sessions/<工作区分桶>/<session-id>/session.v4.jsonl.zstd
  文件是多帧 zstd 拼接；优先用 zstandard 包，缺失时回退到 Node 脚本
  （Node 24 自带 zstd，且必须逐帧解压，见 tools/zstd_frames.mjs）。

抽取策略
  规则版：中文句式匹配（偏好 / 要求 / 回避 / 事实）。
  规则只负责「凑够能跑通」，真正的语义抽取应换成 LLM（见 prompts/extract.md）。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from consolidate import Candidate
from store import ms_to_iso, utc_now

HERE = Path(__file__).resolve().parent
FRAME_TOOL = HERE / "tools" / "zstd_frames.mjs"

DEFAULT_SESSIONS_ROOT = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "sessions"

# 只认真人说的话：DSH 的运行时上下文也是 user/message，但 source.kind 不同
USER_SOURCE_KINDS = {"user"}


# --------------------------------------------------------------------------- #
# 定位与解压
# --------------------------------------------------------------------------- #

def find_session_files(root: Path | None = None) -> list[Path]:
    base = Path(root) if root is not None else DEFAULT_SESSIONS_ROOT
    if not base.exists():
        return []
    return sorted(
        base.glob("*/*/session.v4.jsonl.zstd"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _node_candidates() -> list[str]:
    """按可信度排序的 Node 候选。DSH 自带 Node 24 一定支持 zstd，优先用它。"""
    seen: set[str] = set()
    out: list[str] = []

    def add(p: str | None) -> None:
        if p and p not in seen and Path(p).exists():
            seen.add(p)
            out.append(p)

    add(os.environ.get("DSH_NODE"))
    add(
        str(
            Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
            / "dsh-runtimes"
            / "dsh-primary-runtime"
            / "dependencies"
            / "node"
            / "bin"
            / "node.exe"
        )
    )
    add(shutil.which("node"))
    return out


def _node_binary() -> str | None:
    """历史接口：返回首个候选。"""
    cands = _node_candidates()
    return cands[0] if cands else None


def decompress_session(path: Path) -> str:
    """解压会话日志为 JSONL 文本；兼容多帧。"""
    try:
        import zstandard  # type: ignore

        dctx = zstandard.ZstdDecompressor()
        with open(path, "rb") as fh:
            with dctx.stream_reader(fh, read_across_frames=True) as reader:
                return io.TextIOWrapper(reader, encoding="utf-8").read()
    except ImportError:
        pass

    errors: list[str] = []
    for node in _node_candidates():
        try:
            proc = subprocess.run(
                [node, str(FRAME_TOOL), str(path)],
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            errors.append(f"{node}: {exc}")
            continue
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.decode("utf-8", errors="replace")
        errors.append(
            f"{node} (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace')[:200]}"
        )

    raise RuntimeError(
        "解压失败：需要 zstandard 包，或支持 zstd 的 Node（>=22.15）。\n"
        + "\n".join(errors)
    )


def iter_events(path: Path) -> Iterator[dict]:
    text = decompress_session(path)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


# --------------------------------------------------------------------------- #
# 抽取规则
# --------------------------------------------------------------------------- #

@dataclass
class _Rule:
    kind: str
    predicate: str
    pattern: re.Pattern[str]
    confidence: float = 0.7


RULES: list[_Rule] = [
    # ── 显式指令：优先级最高 ─────────────────────────────────────────
    _Rule("preference", "must_remember", re.compile(r"记住[:：,，\s]*(.{2,60})")),

    # ── 第一人称偏好 / 事实 ─────────────────────────────────────────
    _Rule("preference", "prefers", re.compile(r"我(?:比较|更|很|最)?(?:喜欢|偏好|习惯|爱用|爱)(.{2,40})")),
    _Rule("preference", "wants", re.compile(r"我(?:希望|想要|要求|需要)(.{2,40})")),
    _Rule("preference", "requires", re.compile(r"(?:以后|今后|之后)(?:都|一律)?(.{2,40})")),
    _Rule("preference", "requires", re.compile(r"(?:请|务必|一定要)(?:始终|一直)?(?:用|使用|保持|按照)(.{2,40})")),
    # 否定：原来只认「不要/别/禁止」，漏掉了最常见的「不用」
    _Rule("preference", "avoids", re.compile(r"(?:不要|别|禁止)(?:再|总是)?(?:用|使用|采用)(.{2,30})")),
    _Rule("preference", "avoids", re.compile(r"我(?:现在|以后)?不(?:再)?(?:用|要|喜欢)(.{2,30})")),
    _Rule("fact", "uses", re.compile(r"我(?:现在|目前)?(?:用|使用)([A-Za-z0-9_\-.+ ]{2,30})")),
    _Rule("fact", "located_in", re.compile(r"我(?:现在|目前)?(?:在|住在|搬到)([\u4e00-\u9fffA-Za-z0-9 ]{2,16})(?:工作|生活|住)?")),

    # ── 陈述式结论（本轮新增）──────────────────────────────────────
    #
    # 为什么必须加：上面 9 条几乎全是**第一人称**句式，而用户真实消息里
    # 大量是陈述句（「这个项目用 SQLite」「测试必须先跑 lint」）。
    # 实测旧规则对 12 条典型结论只识出 3 条（25%），不配 LLM 时基本等于没有记忆。
    #
    # 加规则的原则：**只认高置信度、低歧义的结论句式**，宁可少抽也不要误抽。
    # 抽出来的候选还要过「五问闸门」，过程叙述/碎片/时间锚会被拦掉。

    # 「这个项目 / 该仓库 / 本插件 …… 用/采用/基于 X」
    _Rule("fact", "uses", re.compile(
        r"(?:这个|该|本|此)?(?:项目|仓库|插件|系统|工具|工程|模块|脚本)"
        r"(?:坚持|统一|一律)?(?:用|使用|采用|基于|依赖)"
        r"([A-Za-z0-9_\-./+ ]{2,30})")),
    # 「X 是 Y」——排除疑问与时间词，只取明确的判断句
    _Rule("fact", "is", re.compile(
        r"(?:这个|该|本|此)?(?:项目|仓库|插件|系统)(?:的)?"
        r"[^，。；,;？?]{1,12}?是([^，。；,;？?]{2,24})")),
    # 「必须 / 需要 / 应当 X」——规则类结论
    # 负向断言排除纯态度词：「这个项目必须小心」抽出的「小心」没有任何信息量
    _Rule("reflection", "must", re.compile(
        r"(?:必须|需要|应当|务必|一定要)"
        r"(?!(?:小心|注意|当心|留意|谨慎|认真|仔细|严谨|用心))"
        r"([^，。；,;？?]{2,30})")),
    # 「零 X / 不引入 X / 无 X」——约束类结论
    _Rule("reflection", "constraint", re.compile(
        r"(零第三方依赖|零依赖|不引入[^，。；,;？?]{0,16}|无外部依赖|不依赖[^，。；,;？?]{0,16})")),
    # 「因为 X，所以/才 Y」——因果结论（取原因侧）
    _Rule("episode", "root_cause", re.compile(
        r"(?:因为|由于|原因是|根因是)([^，。；,;？?]{2,30})")),
    # 「X 已修复 / 已解决 / 已完成」——状态类结论
    _Rule("fact", "fixed_status", re.compile(
        r"([^，。；,;？?]{2,18}?)(?:已(?:经)?(?:修复|解决|完成|上线|发布))")),
]

SENTENCE_SPLIT = re.compile(r"[。！？!?\n]")
TRAILING = "的了啊呢吧呀哦。"
LEADING = re.compile(r"^(?:用|使用|采用)\s*")

#: object 里该剥掉的尾部动词短语——「neovim 写代码」要的是 neovim，不是整句话
TAIL_VERBS = (
    "写代码", "写脚本", "写程序", "做开发", "做后端", "做前端",
    "编码", "开发", "工作", "写", "做",
)

#: 「回复用简体中文」这种：真正的值在「用」后面
REPLY_PREFIX = re.compile(r"^[^，。；,;]{0,8}?用([A-Za-z0-9\u4e00-\u9fff_\-+.]{2,24})$")


def _clean(text: str) -> str:
    """归一化抽出来的值。

    早期版本直接把正则捕获组原样返回，于是出现
    「neovim 写代码」「回复用简体中文」这种**带着上下文尾巴**的值。
    它们看着还行，但会让同一属性在不同句子里落到不同的 object，
    巩固层就判不出冲突（「换用 emacs」永远覆盖不掉「neovim 写代码」）。
    """
    text = text.strip(" \t，,。.!！?？;；:：\"'“”‘’（）()")
    text = LEADING.sub("", text)
    text = text.strip().rstrip(TRAILING).strip()

    # ① 中英混排：取英文标识符部分（「neovim 写代码」→「neovim」）
    #    纯英文（如「IntelliJ IDEA」）不受影响，因为尾部没有中文
    mixed = re.match(r"^([A-Za-z0-9_\-./+]+(?: [A-Za-z0-9_\-./+]+)*)\s*[\u4e00-\u9fff]", text)
    if mixed:
        candidate = mixed.group(1).strip()
        if len(candidate) >= 2:
            text = candidate

    # ② 剥掉纯中文的尾部动词短语
    for verb in TAIL_VERBS:
        if text.endswith(verb) and len(text) > len(verb) + 1:
            text = text[: -len(verb)].strip()
            break

    # ③ 「回复用简体中文」→「简体中文」
    reply = REPLY_PREFIX.match(text)
    if reply:
        text = reply.group(1).strip()

    return text.strip()


def extract_from_text(text: str) -> list[tuple[str, str, str]]:
    """从一段用户文本里抽 (kind, predicate, object)。"""
    out: list[tuple[str, str, str]] = []
    for sentence in SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if len(sentence) < 4:
            continue
        for rule in RULES:
            m = rule.pattern.search(sentence)
            if m:
                obj = _clean(m.group(1))
                if len(obj) >= 2:
                    out.append((rule.kind, rule.predicate, obj))
                    break
            # 特殊：我的 X 是 Y
            m2 = re.search(r"我的([\u4e00-\u9fff]{2,8})(?:是|叫)(.{1,24})", sentence)
            if m2:
                key = _clean(m2.group(1))
                val = _clean(m2.group(2))
                if key and val:
                    out.append(("fact", f"my_{key}", val))
                    break
    return out


# --------------------------------------------------------------------------- #
# 事件 -> 候选
# --------------------------------------------------------------------------- #

def _message_text(event: dict) -> str:
    data = event.get("data") or {}
    blocks = data.get("content") or []
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _is_human(event: dict) -> bool:
    data = event.get("data") or {}
    source = data.get("source") or {}
    return source.get("kind") in USER_SOURCE_KINDS


def candidates_from_events(events: Iterable[dict], session_id: str) -> list[Candidate]:
    cands: list[Candidate] = []
    for event in events:
        if event.get("type") != "user/message":
            continue
        if not _is_human(event):
            continue
        text = _message_text(event)
        if not text:
            continue
        when = ms_to_iso(event.get("time") or 0) if event.get("time") else utc_now()
        for kind, predicate, obj in extract_from_text(text):
            cands.append(
                Candidate(
                    kind=kind,
                    subject="user",
                    predicate=predicate,
                    object=obj,
                    content=f"用户{kind == 'preference' and '偏好' or '提到'}：{obj}",
                    confidence=0.65,
                    quote=text[:300],
                    source_session=session_id,
                    source_seq=event.get("seq"),
                    recorded_at=when,
                )
            )
        if len(cands) >= 200:  # 单会话上限，防止一次灌爆
            break
    return cands


def load_session(path: Path) -> tuple[str, list[dict]]:
    events = list(iter_events(path))
    session_id = next(
        (e.get("id") for e in events if e.get("type") == "session"),
        path.parent.name,
    )
    return str(session_id), events


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="从 DSH 会话抽取候选记忆")
    parser.add_argument("--session", help="会话 id 或 jsonl.zstd 路径")
    parser.add_argument("--latest", action="store_true", help="取最近修改的会话")
    parser.add_argument("--from-text", help="直接从一段文本抽取（演示/测试用）")
    parser.add_argument("--llm", action="store_true", help="用 LLM 抽取（读 TIDE_MEM_LLM_* 环境变量）")
    parser.add_argument("--batch-size", type=int, default=12, help="LLM 抽取的批大小")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args(argv)

    if args.from_text:
        cands = [
            Candidate(
                kind=kind,
                subject="user",
                predicate=predicate,
                object=obj,
                content=f"用户提到：{obj}",
                confidence=0.65,
                quote=args.from_text[:300],
                source_session="inline",
                recorded_at=utc_now(),
            )
            for kind, predicate, obj in extract_from_text(args.from_text)
        ]
        if args.json:
            print(json.dumps([c.to_json() for c in cands], ensure_ascii=False, indent=2))
        else:
            print(f"从文本抽取 {len(cands)} 条候选：")
            for c in cands:
                print(f"  [{c.kind:<10}] {c.predicate:<14} {c.object}")
        return 0

    if args.session and Path(args.session).exists():
        path = Path(args.session)
    elif args.session:
        matches = [p for p in find_session_files() if path_contains(p, args.session)]
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

    if args.llm:
        from extract_llm import extract, messages_from_events
        from llm import LlmClient, load_llm_config

        cfg = load_llm_config()
        if cfg is None:
            print("未配置 LLM（llm.json 或 TIDE_MEM_LLM_* 环境变量），回退规则抽取。", file=sys.stderr)
        client = LlmClient(cfg) if cfg else None
        report = extract(
            messages_from_events(events),
            session_id=session_id,
            client=client,
            batch_size=args.batch_size,
        )
        cands = report.candidates[: args.limit]
        if report.used_llm:
            print(
                f"LLM 抽取：{report.batches} 批，丢弃 {report.dropped} 条不合格输出",
                file=sys.stderr,
            )
    else:
        cands = candidates_from_events(events, session_id)[: args.limit]

    if args.json:
        print(json.dumps([c.to_json() for c in cands], ensure_ascii=False, indent=2))
    else:
        print(f"会话 {session_id}（{path.name}）共 {len(events)} 个事件，抽取 {len(cands)} 条候选：")
        for c in cands:
            print(f"  [{c.kind:<10}] {c.predicate:<14} {c.object}   (seq={c.source_seq})")
    return 0


def path_contains(path: Path, needle: str) -> bool:
    return needle in str(path).replace("\\", "/")


if __name__ == "__main__":
    raise SystemExit(main())
