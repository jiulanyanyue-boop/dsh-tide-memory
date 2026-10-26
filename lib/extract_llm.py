"""LLM 抽取器：把会话消息变成候选记忆，带严格校验和规则回退。

设计原则
  - 模型的输出一律当「不可信输入」：字段白名单、长度上限、seq 校验，
    不合格的条目直接丢弃，绝不因为模型自信就放进记忆库。
  - 任何 LLM 故障都回退到规则抽取，保证批处理不会整晚跑空。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from consolidate import Candidate, fingerprint
from extract_dsh import extract_from_text
from llm import LlmClient, LlmError
from store import ms_to_iso, utc_now

HERE = Path(__file__).resolve().parent
EXTRACT_PROMPT = HERE / "prompts" / "extract.md"

KINDS = ("preference", "fact", "episode", "reflection")
PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
MAX_OBJECT = 80
MAX_CONTENT = 240
MAX_QUOTE = 300


@dataclass
class ExtractReport:
    candidates: list[Candidate]
    dropped: int = 0
    batches: int = 0
    used_llm: bool = False
    note: str = ""


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #

def _clean_str(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def normalize_predicate(predicate: str, obj: str) -> str:
    """把「值」从谓词里剥掉，保证同一个属性永远落到同一个 key。

    模型很爱写 `prefers_neovim` / `db_engine_sqlite`，这样「改用 emacs」
    就会落到不同的 key 上，冲突永远判不出来。这里按 object 的英文 token
    删掉谓词尾部的值部分；中文 object 不参与（避免误伤）。
    """
    parts = predicate.split("_")
    obj_tokens = {
        tok for tok in re.split(r"[^a-z0-9]+", obj.lower()) if len(tok) >= 2
    }
    if not obj_tokens or len(parts) < 2:
        return predicate
    while len(parts) > 1 and parts[-1] in obj_tokens:
        parts.pop()
    return "_".join(parts) or predicate


def _infer_seq(quote: str, seq_texts: dict[int, str]) -> int | None:
    """模型编不出正确的 seq 时，用 quote 反查原文消息。"""
    q = (quote or "").strip()
    if not q:
        return None
    for seq, text in seq_texts.items():
        if q in text:
            return seq
    return None


def _coerce_confidence(value: Any, default: float = 0.5) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, num))


def parse_candidates(
    payload: Any,
    *,
    session_id: str,
    seq_texts: dict[int, str],
) -> tuple[list[Candidate], int]:
    """把模型输出变成 Candidate；返回 (候选列表, 丢弃数)。

    seq_texts: 输入消息的 {seq: 原文}，用于校验/反查 source_seq。
    """
    if isinstance(payload, dict):
        # 兼容两种形态：{"candidates": [...]} 或直接单条候选对象
        if isinstance(payload.get("candidates"), list):
            payload = payload["candidates"]
        elif {"kind", "predicate", "object"} & payload.keys():
            payload = [payload]
        else:
            payload = []
    if not isinstance(payload, list):
        return [], 1

    out: list[Candidate] = []
    dropped = 0
    for item in payload:
        if not isinstance(item, dict):
            dropped += 1
            continue
        kind = str(item.get("kind", "")).strip()
        raw_predicate = str(item.get("predicate", "")).strip().lower()
        obj = _clean_str(item.get("object"), MAX_OBJECT)
        content = _clean_str(item.get("content"), MAX_CONTENT) or obj
        quote = _clean_str(item.get("quote"), MAX_QUOTE)
        predicate = normalize_predicate(raw_predicate, obj)

        if kind not in KINDS or not PREDICATE_RE.match(predicate) or len(obj) < 2:
            dropped += 1
            continue

        seq_raw = item.get("source_seq")
        seq: int | None
        try:
            seq = int(seq_raw) if seq_raw is not None else None
        except (TypeError, ValueError):
            seq = None
        if seq is not None and seq not in seq_texts:
            seq = None  # 模型编的 seq，不采信
        if seq is None:
            seq = _infer_seq(quote or content, seq_texts)

        out.append(
            Candidate(
                kind=kind,
                subject=_clean_str(item.get("subject"), 40) or "user",
                predicate=predicate,
                object=obj,
                content=content,
                confidence=_coerce_confidence(item.get("confidence"), 0.5),
                quote=quote,
                source_session=session_id,
                source_seq=seq,
            )
        )
    return out, dropped


def _dedupe(cands: Iterable[Candidate]) -> list[Candidate]:
    """同指纹只留置信度最高的一条（同一次抽取内的去重）。"""
    best: dict[str, Candidate] = {}
    for cand in cands:
        fp = fingerprint(cand.kind, cand.subject, cand.predicate, cand.object)
        if fp not in best or cand.confidence > best[fp].confidence:
            best[fp] = cand
    return list(best.values())


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #

def extract(
    messages: list[dict],
    *,
    session_id: str,
    client: LlmClient | None = None,
    prompt: str | None = None,
    batch_size: int = 12,
    fallback: bool = True,
    cwd: str = '',
) -> ExtractReport:
    """messages: [{'seq': int, 'time': '...', 'text': str}]"""
    if not messages:
        return ExtractReport([])

    seq_texts = {
        m["seq"]: m.get("text", "") for m in messages if m.get("seq") is not None
    }
    if client is None:
        if not fallback:
            return ExtractReport([])
        return ExtractReport(_rule_fallback(messages, session_id, cwd), note="未配置 LLM，使用规则抽取")

    system_prompt = prompt or EXTRACT_PROMPT.read_text(encoding="utf-8")
    collected: list[Candidate] = []
    dropped = 0
    batches = 0

    for start in range(0, len(messages), batch_size):
        chunk = messages[start : start + batch_size]
        batches += 1
        payload = {"session_id": session_id, "messages": chunk}
        try:
            raw = client.chat_json(system_prompt, payload)
        except LlmError as exc:
            if fallback:
                rule = _rule_fallback(chunk, session_id, cwd)
                collected.extend(rule)
                dropped += 1
                continue
            raise
        got, bad = parse_candidates(raw, session_id=session_id, seq_texts=seq_texts)
        collected.extend(got)
        dropped += bad

    return ExtractReport(
        candidates=_dedupe(collected),
        dropped=dropped,
        batches=batches,
        used_llm=True,
        note="LLM 抽取",
    )


def _rule_fallback(messages: list[dict], session_id: str, cwd: str = "") -> list[Candidate]:
    """规则兜底抽取。cwd 决定归属——降级路径同样不能丢掉项目信息。"""
    from memory_semantics import resolve_scope_from_cwd

    out: list[Candidate] = []
    for msg in messages:
        text = msg.get("text", "")
        when = msg.get("time") or None
        msg_cwd = msg.get("cwd") or cwd or ""
        for kind, predicate, obj in extract_from_text(text):
            sc = resolve_scope_from_cwd(predicate=predicate, subject="user", cwd=msg_cwd)
            out.append(
                Candidate(
                    kind=kind,
                    subject="user",
                    predicate=predicate,
                    object=obj,
                    content=f"用户提到：{obj}",
                    confidence=0.55,
                    quote=text[:MAX_QUOTE],
                    source_session=session_id,
                    source_seq=msg.get("seq"),
                    recorded_at=when or utc_now(),
                    scope=sc.scope,
                    scope_ref=sc.scope_ref,
                    cwd=msg_cwd,
                )
            )
    return out


# --------------------------------------------------------------------------- #
# 从事件构造消息
# --------------------------------------------------------------------------- #

def messages_from_events(events: list[dict]) -> list[dict]:
    """只取真人消息；带上 seq / 时间，作为抽取输入。"""
    out: list[dict] = []
    for event in events:
        if event.get("type") != "user/message":
            continue
        data = event.get("data") or {}
        source = data.get("source") or {}
        if source.get("kind") != "user":
            continue
        blocks = data.get("content") or []
        text = "\n".join(
            b.get("text", "") for b in blocks if b.get("type") == "text"
        ).strip()
        if not text:
            continue
        when = ms_to_iso(event["time"]) if event.get("time") else None
        out.append({"seq": event.get("seq"), "time": when, "text": text})
    return out
