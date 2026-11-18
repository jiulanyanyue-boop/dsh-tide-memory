"""记忆里的凭据通道：识别敏感值、加密保管、只让掩码进记忆库。

为什么需要这个模块
  v2.3.0 的记忆库对「服务器 IP / 账号 / 密码」完全没有概念：
  正则抽取器压根不认这些值（该记的不记），LLM 抽出来又会原样落进
  memory.db（不该存的明文存了）。而 LLM 自身在遇到明文口令时会返回
  `{"status":"blocked"}`，`parse_candidates` 对没有 kind 的字典又直接丢弃，
  于是账密这条路两个方向都是断的。

设计取舍（按主人选定：加密存储 + 界面掩码）
  · IP、端口、账号 → **明文**存进记忆库。它们本身是复用价值最高的环境事实，
    且脱敏之后就没法用了。
  · 密码 / token / 私钥 / 连接串口令 → **密文**存进 credentials.json
    （DPAPI，绑定当前 Windows 用户），记忆库里只留一个 `cred://<id>` 指针
    和掩码尾巴。查得到「配过这个凭据」，但明文永远不落 memory.db。
  · 密文文件与记忆库物理分离：备份/导出/注入上下文时天然不会带出明文。

与 secret_store.py 的关系
  它管的是「LLM API Key」这一个用途，写死在 llm.secret。这里管的是「任意条目的
  凭据」，需要多值、带标签，所以另起一个文件，但**复用它的加密实现**——
  DPAPI 绑定、AES-GCM 退路、ACL 收紧全部照搬，不重新发明一遍。
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import secret_store

#: 密文文件版本（结构变化时递增，方便未来迁移）
CRED_VERSION = 1

#: 记忆库里指向凭据的协议前缀
POINTER_PREFIX = "cred://"


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #

def _data_dir() -> Path:
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory"


def cred_path() -> Path:
    explicit = os.environ.get("TIDE_MEM_CRED_FILE")
    return Path(explicit) if explicit else _data_dir() / "credentials.json"


# --------------------------------------------------------------------------- #
# 敏感值识别
# --------------------------------------------------------------------------- #

#: 「标签 + 分隔符 + 值」形式的凭据。
#: 分隔符可以是 `是/为/:/=/空白`——中文里「密码 P@ssw0rd123」直接空格接值很常见，
#: 只认冒号会漏掉一大半真实说法。值抓到空白或中文标点为止。
_LABELED_SECRET = re.compile(
    r"(?:密码|口令|passwd|password|pwd|pass|token|密钥|secret|api[_-]?key|access[_-]?key)"
    r"\s*(?:是|为|[:：=])?\s*"
    r"([^\s，,。；;、）)\]】\"'“”]{3,120})",
    re.IGNORECASE,
)

#: 形如 `user/password` 的账号密码对（LLM 常把凭据压成一个 object）
_SLASH_PAIR = re.compile(r"^([A-Za-z0-9_.\-]{1,64})/(\S{3,120})$")

#: 常见密钥形态：sk-xxx / AKIA... / ghp_xxx / Bearer xxx
_KEY_SHAPES = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{12,}|AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,}"
    r"|xox[bap]-[A-Za-z0-9\-]{10,})\b"
)

#: 明确不是凭据的「密码类」词——避免把「密码策略」「密码框」当值抓走
_NOT_SECRET_VALUES = {
    "策略", "规则", "框", "输入框", "强度", "长度", "错误", "提示", "字段",
    "校验", "忘记", "重置", "修改", "设置", "为空", "不能为空", "太弱",
    "错误提示", "输入", "页面", "界面", "管理器", "生成器", "泄露", "过期",
}

#: 值里必须至少出现一个 ASCII 字母或数字。
#: 没有这条，「密码策略要复杂一点」会把整个「策略要复杂一点」当成密码。
_HAS_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")


def _looks_like_secret_value(value: str) -> bool:
    """值像不像一个真凭据。宁可漏，不可错：错一次记忆就彻底不可读了。"""
    v = (value or "").strip()
    if len(v) < 3:
        return False
    if v in _NOT_SECRET_VALUES:
        return False
    # 纯中文不是凭据（「密码策略要复杂一点」）
    if not _HAS_ASCII_ALNUM.search(v):
        return False
    # 以「的/了/吗/呢」等虚词结尾，说明是把句子截断了
    if v[-1] in "的了呢吗吧呀啊":
        return False
    return True


def find_secrets(text: str) -> list[tuple[str, str]]:
    """从一段文本里找出 (标签, 明文值)。

    只认高置信度的「明确的凭据赋值」形态。宁可漏，不可错——
    把普通名词当密码加密会让记忆彻底不可读。
    """
    found: list[tuple[str, str]] = []
    body = text or ""
    seen: set[str] = set()

    def _add(label: str, value: str) -> None:
        if value in seen:
            return
        seen.add(value)
        found.append((label, value))

    for match in _LABELED_SECRET.finditer(body):
        value = match.group(1).strip()
        if not _looks_like_secret_value(value):
            continue
        # 「密码是 XXX」里 label 在 group(0) 中值之前
        prefix = match.group(0)[: match.group(0).find(match.group(1))]
        label = prefix.strip(" :：=是为") or "密码"
        _add(label, value)

    for match in _KEY_SHAPES.finditer(body):
        _add("密钥", match.group(1))

    # `user/password` 压成一个值的情况（常见于 LLM 输出的 object）
    pair = _SLASH_PAIR.match(body.strip())
    if pair and _looks_like_secret_value(pair.group(2)):
        _add("账号密码对", pair.group(2))

    return found


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))


def _mask(value: str) -> str:
    """给界面看的形态：只露末 2 位。

    密码通常比 API Key 短得多，露 4 位风险偏高，这里收紧到 2 位；
    短于 6 位的一律全遮——「ab…」等于把答案说了一半。
    """
    value = value or ""
    if len(value) < 6:
        return "…"
    return "…" + value[-2:]


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #

def _load_document() -> dict[str, Any]:
    target = cred_path()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"v": CRED_VERSION, "items": {}}
    except (OSError, json.JSONDecodeError):
        # 坏文件不能静默清空——那样等于丢主人的凭据。显式报错让上层降级。
        raise secret_store.SecretError(f"凭据文件不可读：{target}")
    if not isinstance(document, dict):
        raise secret_store.SecretError("凭据文件结构不合法")
    document.setdefault("items", {})
    return document


def _save_document(document: dict[str, Any]) -> None:
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    secret_store._atomic_write(cred_path(), payload.encode("utf-8"))


def _encrypt(plaintext: str) -> tuple[str, str]:
    """返回 (scheme, base64 密文)。加密失败就抛——绝不退化成明文落盘。"""
    scheme = secret_store.active_scheme()
    raw = plaintext.encode("utf-8")
    if scheme == secret_store.SCHEME_DPAPI:
        blob = secret_store._dpapi_transform(raw, encrypt=True)
    elif scheme == secret_store.SCHEME_AESGCM_FILE:
        blob = secret_store._aesgcm_encrypt(secret_store._load_or_create_master_key(), raw)
    else:
        raise secret_store.SecretError("没有可用的加密方案，拒绝以明文保存凭据")
    return scheme, base64.b64encode(blob).decode("ascii")


def _decrypt(scheme: str, cipher_b64: str) -> str:
    blob = base64.b64decode(cipher_b64.encode("ascii"), validate=True)
    if scheme == secret_store.SCHEME_DPAPI:
        raw = secret_store._dpapi_transform(blob, encrypt=False)
    elif scheme == secret_store.SCHEME_AESGCM_FILE:
        raw = secret_store._aesgcm_decrypt(secret_store._load_or_create_master_key(), blob)
    else:
        raise secret_store.SecretError(f"未知加密方案：{scheme}")
    return raw.decode("utf-8")


def save_credential(
    plaintext: str,
    *,
    label: str,
    hint: str = "",
    scope: str = "",
    scope_ref: str = "",
) -> str:
    """加密保存一个凭据，返回它的 id（记忆库里存 `cred://<id>`）。

    ⚠️ hint 是「这条凭据的上下文提示」，会被写进**明文**的 credentials.json。
    所以传进来之前必须先脱敏：调用方把明文值替换成掩码再给它。
    这里再兜一层——凡是被识别成凭据的值，一律从 hint 里抹掉。
    早期版本直接把原文塞进 hint，等于把密文旁边放了张写着答案的便签。
    """
    value = (plaintext or "").strip()
    if not value:
        raise secret_store.SecretError("空凭据不能保存")

    safe_hint = _scrub_hint(hint, value)

    scheme, cipher = _encrypt(value)
    cred_id = "cred_" + secrets.token_hex(8)
    document = _load_document()
    document["items"][cred_id] = {
        "label": label or "凭据",
        "hint": safe_hint,
        "scheme": scheme,
        "cipher": cipher,
        "masked": _mask(value),
        "scope": scope,
        "scope_ref": scope_ref,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    document["v"] = CRED_VERSION
    _save_document(document)
    return cred_id


def _scrub_hint(hint: str, value: str) -> str:
    """把 hint 里的明文值抹成本文值之外的安全形态。

    规则：本凭据的值、以及 hint 里被识别出的任何其他凭据值，全部替换成掩码。
    """
    text = (hint or "").strip()
    if not text:
        return ""
    replacements: list[str] = []
    if value:
        replacements.append(value)
    for _label, other in find_secrets(text):
        if other and other not in replacements:
            replacements.append(other)
    # 长的先替换，避免「P@ssw0rd123」被「P@ssw0rd」抢先切碎
    for secret_value in sorted(replacements, key=len, reverse=True):
        if secret_value:
            text = text.replace(secret_value, _mask(secret_value))
    return text[:200]


def load_credential(cred_id: str) -> tuple[str, dict[str, Any]]:
    """解出明文。只在用户明确要「查看凭据」时调用，结果绝不进任何响应体。"""
    key = str(cred_id or "").strip()
    if key.startswith(POINTER_PREFIX):
        key = key[len(POINTER_PREFIX):]
    item = _load_document()["items"].get(key)
    if not isinstance(item, dict):
        raise secret_store.SecretError(f"凭据不存在：{key}")
    return _decrypt(str(item.get("scheme") or ""), str(item.get("cipher") or "")), item


def describe_credential(cred_id: str) -> dict[str, Any]:
    """给面板看的状态：掩码 + 标签，**不含明文**。"""
    key = str(cred_id or "").strip()
    if key.startswith(POINTER_PREFIX):
        key = key[len(POINTER_PREFIX):]
    item = _load_document()["items"].get(key)
    if not isinstance(item, dict):
        return {"id": key, "exists": False}
    return {
        "id": key,
        "exists": True,
        "label": item.get("label", ""),
        "hint": item.get("hint", ""),
        "masked": item.get("masked", "…"),
        "created_at": item.get("created_at", ""),
    }


def is_pointer(text: str) -> bool:
    return str(text or "").strip().startswith(POINTER_PREFIX)


def masked_of(text: str) -> str:
    """把 `cred://xxx` 或明文字符串转成可展示的掩码。"""
    raw = str(text or "").strip()
    if is_pointer(raw):
        info = describe_credential(raw)
        return info.get("masked", "…") if info.get("exists") else "…（凭据已丢失）"
    return _mask(raw)


# --------------------------------------------------------------------------- #
# 与候选记忆的衔接
# --------------------------------------------------------------------------- #

def redact_candidate(cand) -> tuple[Any, list[dict[str, Any]]]:
    """把候选里的明文凭据换成指针 + 掩码。

    Returns (候选, 脱敏记录列表)。脱敏记录用于审计，只含掩码，不含明文。

    为什么在「候选」这一层做而不是在抽取器里做：
      抽取器有多条路径（LLM / 规则 / 设置页手动添加），在更靠后的统一层拦一道，
      才能保证不管从哪儿来的凭据都不会漏进 memory.db。
    """
    from consolidate import Candidate  # 局部导入，避免循环依赖

    hits: list[dict[str, Any]] = []
    obj = str(getattr(cand, "object", "") or "")
    content = str(getattr(cand, "content", "") or "")

    def _swap(text: str) -> str:
        """把 text 里的明文凭据替换成指针。"""
        out = text
        secrets_found = find_secrets(out)
        # 「user/password」这种压在一起的，也拆开处理
        pair = _SLASH_PAIR.match(out.strip())
        if pair and not secrets_found:
            secrets_found = [("账号密码对", pair.group(2))]
        for label, value in secrets_found:
            try:
                cred_id = save_credential(
                    value, label=label, hint=out[:120],
                    scope=getattr(cand, "scope", "") or "",
                    scope_ref=getattr(cand, "scope_ref", "") or "",
                )
            except secret_store.SecretError:
                # 加密不可用：宁可把这条值抹掉，也绝不明文落库
                out = out.replace(value, "…（凭据未保存：加密不可用）")
                hits.append({"label": label, "masked": "…", "stored": False})
                continue
            out = out.replace(value, f"{POINTER_PREFIX}{cred_id}")
            hits.append({
                "label": label,
                "masked": masked_of(f"{POINTER_PREFIX}{cred_id}"),
                "cred_id": cred_id,
                "stored": True,
            })
        return out

    new_obj = _swap(obj)
    new_content = _swap(content)
    if not hits:
        return cand, hits

    # content 若等于 object 的复读，替换后可能变成同一串指针，仍保留原句结构
    cand = Candidate(
        kind=cand.kind,
        subject=cand.subject,
        predicate=cand.predicate,
        object=new_obj,
        content=new_content,
        confidence=cand.confidence,
        quote="",  # 证据里的 quote 可能整段含明文，脱敏后不留原文
        source_session=cand.source_session,
        source_seq=cand.source_seq,
        recorded_at=cand.recorded_at,
        scope=cand.scope,
        scope_ref=cand.scope_ref,
        summary=cand.summary,
        depends_on=cand.depends_on,
        cwd=cand.cwd,
        mentioned_path=cand.mentioned_path,
    )
    return cand, hits
