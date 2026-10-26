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
    # 显式指令优先级最高
    _Rule("preference", "must_remember", re.compile(r"记住[:：,，\s]*(.{2,60})")),
    _Rule("preference", "prefers", re.compile(r"我(?:比较|更|很|最)?(?:喜欢|偏好|习惯|爱用|爱)(.{2,40})")),
    _Rule("preference", "wants", re.compile(r"我(?:希望|想要|要求|需要)(.{2,40})")),
    _Rule("preference", "requires", re.compile(r"(?:以后|今后|之后)(?:都|一律)?(.{2,40})")),
    _Rule("preference", "requires", re.compile(r"(?:请|务必|一定要)(?:始终|一直)?(?:用|使用|保持|按照)(.{2,40})")),
    _Rule("preference", "avoids", re.compile(r"(?:不要|别|禁止)(?:再|总是)?(?:用|使用|采用)(.{2,30})")),
    _Rule("fact", "uses", re.compile(r"我(?:现在|目前)?(?:用|使用)([A-Za-z0-9_\-.+ ]{2,30})")),
    _Rule("fact", "located_in", re.compile(r"我(?:现在|目前)?(?:在|住在|搬到)([\u4e00-\u9fffA-Za-z0-9 ]{2,16})(?:工作|生活|住)?")),
]

SENTENCE_SPLIT = re.compile(r"[。！？!?\n]")
TRAILING = "的了啊呢吧呀哦。"
LEADING = re.compile(r"^(?:用|使用|采用)\s*")


def _clean(text: str) -> str:
    text = text.strip(" \t，,。.!！?？;；:：\"'“”‘’（）()")
    text = LEADING.sub("", text)
    return text.strip().rstrip(TRAILING).strip()


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
