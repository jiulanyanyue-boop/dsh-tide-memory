"""OpenAI 兼容的 LLM 客户端（零依赖，urllib 实现）。

只用得到一件事：给一个 system prompt + 一段 JSON 输入，拿回一个 JSON 对象/数组。
带超时、重试、以及「模型把 JSON 包在 markdown 里」的容错解析。
"""

from __future__ import annotations

import json
import ipaddress
import os
import re
import time
import urllib.error
import urllib.request
import ctypes
import ctypes.wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class LlmError(RuntimeError):
    """LLM 调用失败（超时、网络、非法响应）。调用方据此回退。"""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward bearer credentials to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_llm_request(request: urllib.request.Request, timeout: float):
    """Open an authenticated LLM request without following redirects."""
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def validate_base_url(base_url: str) -> str:
    """Require TLS remotely while preserving HTTP for local Ollama endpoints."""
    try:
        parsed = urlsplit(base_url.strip())
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise LlmError(f"LLM base URL 无效：{exc}") from exc

    if not parsed.netloc or not hostname or parsed.username is not None or parsed.password is not None:
        raise LlmError("LLM base URL 必须是无凭据的绝对 URL")
    if parsed.query or parsed.fragment:
        raise LlmError("LLM base URL 不能包含 query 或 fragment")
    if parsed.scheme.lower() == "https":
        return base_url.rstrip("/")
    if parsed.scheme.lower() != "http":
        raise LlmError("LLM base URL 只支持 HTTPS；HTTP 仅支持 loopback")

    host = hostname.rstrip(".").lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise LlmError("拒绝向非本机 HTTP LLM 端点发送请求；请使用 HTTPS")
    return base_url.rstrip("/")


PROJECT_ROOT = Path(__file__).resolve().parent


def _default_config_path() -> Path:
    """LLM 配置默认位置：$DSH_HOME/dsh-tide-memory/llm.json。

    **不放插件包内部**：插件目录可能只读、升级时会被替换，
    而且把密钥写进安装目录是安全隐患。
    """
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory" / "llm.json"


CONFIG_PATH = Path(os.environ.get("TIDE_MEM_LLM_CONFIG", _default_config_path()))


@dataclass
class LlmConfig:
    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 90.0
    retries: int = 1

    @classmethod
    def from_env(cls, prefix: str = "MEM_LLM") -> "LlmConfig | None":
        base = os.environ.get(f"{prefix}_BASE_URL")
        model = os.environ.get(f"{prefix}_MODEL")
        if not (base and model):
            return None
        return cls(
            base_url=base.rstrip("/"),
            model=model,
            api_key=os.environ.get(f"{prefix}_API_KEY", ""),
            timeout=float(os.environ.get(f"{prefix}_TIMEOUT", "90")),
            retries=int(os.environ.get(f"{prefix}_RETRIES", "1")),
        )


def load_llm_config(path: str | Path | None = None) -> LlmConfig | None:
    """环境变量优先，其次 memory-consolidation/llm.json。都没有返回 None。"""
    cfg = LlmConfig.from_env()
    if cfg is not None:
        return cfg

    target = Path(path) if path else CONFIG_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    base = str(data.get("base_url", "")).strip()
    model = str(data.get("model", "")).strip()
    if not base or not model:
        return None
    return LlmConfig(
        base_url=base.rstrip("/"),
        model=model,
        api_key=str(data.get("api_key", "")),
        timeout=float(data.get("timeout", 90)),
        retries=int(data.get("retries", 1)),
    )


def save_llm_config(config: LlmConfig, path: str | Path | None = None) -> Path:
    target = Path(path) if path else CONFIG_PATH
    payload = {
        "base_url": config.base_url,
        "model": config.model,
        "api_key": config.api_key,
        "timeout": config.timeout,
        "retries": config.retries,
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        if os.name == "nt":
            _restrict_windows_file_acl(target)
        else:
            # Permission failure must be visible; do not silently claim the key is protected.
            os.chmod(target, 0o600)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def _restrict_windows_file_acl(target: Path) -> None:
    """Protect the config DACL for the current user, SYSTEM and administrators."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    bool_type = ctypes.wintypes.BOOL
    handle_type = ctypes.wintypes.HANDLE
    dword_type = ctypes.wintypes.DWORD
    advapi32.OpenProcessToken.argtypes = [handle_type, dword_type, ctypes.POINTER(handle_type)]
    advapi32.OpenProcessToken.restype = bool_type
    advapi32.GetTokenInformation.argtypes = [handle_type, ctypes.c_int, ctypes.c_void_p, dword_type, ctypes.POINTER(dword_type)]
    advapi32.GetTokenInformation.restype = bool_type
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = bool_type
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [ctypes.wintypes.LPCWSTR, dword_type, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(dword_type)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = bool_type
    advapi32.SetFileSecurityW.argtypes = [ctypes.wintypes.LPCWSTR, dword_type, ctypes.c_void_p]
    advapi32.SetFileSecurityW.restype = bool_type
    kernel32.GetCurrentProcess.restype = handle_type
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [handle_type]
    kernel32.CloseHandle.restype = bool_type
    token = ctypes.wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = ctypes.wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, 1, token_user, needed.value, ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_pointer = ctypes.cast(token_user, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_string = ctypes.wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sddl = f"D:P(A;;FA;;;{sid_string.value})(A;;FA;;;SY)(A;;FA;;;BA)"
        finally:
            kernel32.LocalFree(sid_string)
        descriptor = ctypes.c_void_p()
        descriptor_size = ctypes.wintypes.DWORD()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not advapi32.SetFileSecurityW(
                str(target), 0x00000004 | 0x80000000, descriptor
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.LocalFree(descriptor)
    finally:
        kernel32.CloseHandle(token)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """从模型输出里抠出 JSON：裸 JSON -> 代码块 -> 第一个括号片段。"""
    text = (text or "").strip()
    if not text:
        raise LlmError("模型返回空内容")

    for candidate in (text, *_JSON_BLOCK.findall(text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # 退而求其次：从第一个 [ 或 { 开始做括号配平
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise LlmError(f"无法从模型输出中解析 JSON：{text[:160]!r}")


class LlmClient:
    def __init__(self, config: LlmConfig) -> None:
        self.config = config
        self.base_url = validate_base_url(config.base_url)

    def chat_json(self, system: str, user: Any) -> Any:
        payload = {
            "model": self.config.model,
            "temperature": 0,
            # 不用 response_format=json_object：Ollama 会强制顶层为对象，
            # 而抽取要的是数组。靠 extract_json 从输出里抠 JSON 更稳。
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(user, ensure_ascii=False),
                },
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        last_err: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with open_llm_request(req, timeout=self.config.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                return extract_json(content)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
            except (KeyError, IndexError, json.JSONDecodeError, LlmError) as exc:
                last_err = exc
            if attempt < self.config.retries:
                time.sleep(1.5 * (attempt + 1))

        raise LlmError(f"LLM 调用失败：{last_err}")
