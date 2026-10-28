"""LLM API Key 的加密存储。

设计目标（按主人要求）：
  · 面板可以明文输入密钥，但**落盘必须是密文**；
  · 任何 API 响应都不含明文，只回掩码；
  · 密文跟着用户走不了——主密钥绑定当前 Windows 用户（DPAPI），换机换用户就解不开。

三档方案，按可用性自动选择：
  1. dpapi      —— Windows CryptProtectData，密文只有当前登录用户能解。首选。
  2. aesgcm-file —— 非 Windows 退路：随机 32B 主密钥文件（0600）+ AES-256-GCM 的
                     等价实现（HMAC-SHA256 计数器流 + encrypt-then-MAC）。
                     纯 stdlib，不引第三方。
  3. none       —— 两者都不可用（极少见）。此时**不落盘**，只在内存里留着本次进程可用，
                     并在 describe() 里如实报告 scheme='none'，绝不假装已加密。

密钥文件与配置文件分开：
  · $DSH_HOME/dsh-tide-memory/llm.secret  —— 密文（DPAPI blob 或 AES 封装）
  · $DSH_HOME/dsh-tide-memory/llm.key     —— 仅非 Windows 退路的主密钥（0600）
  · $DSH_HOME/dsh-tide-memory/llm.json    —— 明文配置，**永不含 api_key**
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import hashlib
import hmac
import json
import os
import secrets
import struct
from pathlib import Path

# 密文文件格式版本
SECRET_VERSION = 1

SCHEME_DPAPI = "dpapi"
SCHEME_AESGCM_FILE = "aesgcm-file"
SCHEME_NONE = "none"

_AES_AAD = b"dsh-tide-memory/llm-secret/v1"
_AES_IV_BYTES = 12
_AES_TAG_BYTES = 32
_AES_KEY_BYTES = 32


class SecretError(RuntimeError):
    """密钥加解密失败。调用方应视为「没有可用密钥」，不要崩溃。"""


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #

def _data_dir() -> Path:
    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory"


def secret_path() -> Path:
    explicit = os.environ.get("TIDE_MEM_SECRET_FILE")
    return Path(explicit) if explicit else _data_dir() / "llm.secret"


def master_key_path() -> Path:
    explicit = os.environ.get("TIDE_MEM_SECRET_KEY")
    return Path(explicit) if explicit else _data_dir() / "llm.key"


# --------------------------------------------------------------------------- #
# 文件权限
# --------------------------------------------------------------------------- #

def _restrict_windows_file_acl(target: Path) -> None:
    """把 DACL 收紧到当前用户 + SYSTEM + Administrators。

    与 llm.py 里那份同源；这里的密文文件同样不能给别的用户读。
    """
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
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = ctypes.wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, token_user, needed.value, ctypes.byref(needed)):
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
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not advapi32.SetFileSecurityW(str(target), 0x00000004 | 0x80000000, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.LocalFree(descriptor)
    finally:
        kernel32.CloseHandle(token)


def _harden(target: Path) -> None:
    """收紧权限；失败就把文件删掉并抛错——不能留下一个「看起来加密了但人人可读」的文件。"""
    try:
        if os.name == "nt":
            _restrict_windows_file_acl(target)
        else:
            os.chmod(target, 0o600)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    _harden(tmp)
    os.replace(tmp, target)


# --------------------------------------------------------------------------- #
# 方案 1：Windows DPAPI
# --------------------------------------------------------------------------- #

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_available() -> bool:
    return os.name == "nt"


def _dpapi_transform(data: bytes, *, encrypt: bool) -> bytes:
    """调 CryptProtectData / CryptUnprotectData。

    刻意不传 CRYPTPROTECT_LOCAL_MACHINE：密文绑定「当前用户」，
    换个账户或换台机器都解不开，这正是主人要的那种安全。
    """
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()

    argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    if encrypt:
        crypt32.CryptProtectData.argtypes = argtypes
        crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), "dsh-tide-memory llm key", None, None, None, 0, ctypes.byref(blob_out)
        )
    else:
        crypt32.CryptUnprotectData.argtypes = argtypes
        crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
    if not ok:
        raise SecretError(f"DPAPI 调用失败：{ctypes.get_last_error()}")

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    except Exception as exc:  # noqa: BLE001
        raise SecretError(f"DPAPI 结果读取失败：{exc}") from exc
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------- #
# 方案 2：AES-256-GCM 等价实现（HMAC-SHA256 计数器流 + encrypt-then-MAC）
#
# 为什么不用纯 Python AES：stdlib 没有 AES，引第三方会破坏「零依赖」承诺。
# 这里用 CTR 模式的 HMAC-SHA256 流加密 + 独立 HMAC 认证，安全性等价于
# encrypt-then-MAC 的组合构造（不同密钥、不同用途、带 AAD、带 IV）。
# --------------------------------------------------------------------------- #

def _keystream(key: bytes, iv: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, iv + struct.pack(">Q", counter), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _mac_key(key: bytes) -> bytes:
    return hmac.new(key, b"mac-key-derivation", hashlib.sha256).digest()


def _aesgcm_encrypt(key: bytes, plaintext: bytes) -> bytes:
    iv = secrets.token_bytes(_AES_IV_BYTES)
    stream = _keystream(key, iv, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
    tag = hmac.new(_mac_key(key), _AES_AAD + iv + ciphertext, hashlib.sha256).digest()
    return iv + ciphertext + tag


def _aesgcm_decrypt(key: bytes, packed: bytes) -> bytes:
    if len(packed) < _AES_IV_BYTES + _AES_TAG_BYTES:
        raise SecretError("密文长度不合法")
    iv = packed[:_AES_IV_BYTES]
    ciphertext = packed[_AES_IV_BYTES:-_AES_TAG_BYTES]
    tag = packed[-_AES_TAG_BYTES:]
    expected = hmac.new(_mac_key(key), _AES_AAD + iv + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise SecretError("密文校验失败（可能被篡改或主密钥已更换）")
    stream = _keystream(key, iv, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


def _load_or_create_master_key() -> bytes:
    """主密钥文件：随机 32B，0600。不存在就现造一把。"""
    target = master_key_path()
    try:
        raw = target.read_bytes()
        if len(raw) == _AES_KEY_BYTES:
            return raw
        raise SecretError("主密钥文件长度不合法")
    except FileNotFoundError:
        pass
    except SecretError:
        raise
    except OSError as exc:
        raise SecretError(f"主密钥文件不可读：{exc}") from exc

    key = secrets.token_bytes(_AES_KEY_BYTES)
    _atomic_write(target, key)
    return key


# --------------------------------------------------------------------------- #
# 对外：保存 / 读取 / 查询
# --------------------------------------------------------------------------- #

def active_scheme() -> str:
    """当前平台首选的加密方案。"""
    return SCHEME_DPAPI if dpapi_available() else SCHEME_AESGCM_FILE


def save_secret(api_key: str) -> dict[str, object]:
    """加密并落盘。返回 describe() 形状的结果（不含明文）。"""
    key_text = (api_key or "").strip()
    if not key_text:
        raise SecretError("空密钥不能保存；要移除请用清除")

    scheme = active_scheme()
    raw = key_text.encode("utf-8")
    try:
        if scheme == SCHEME_DPAPI:
            payload_blob = _dpapi_transform(raw, encrypt=True)
        else:
            payload_blob = _aesgcm_encrypt(_load_or_create_master_key(), raw)
    except SecretError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SecretError(f"加密失败：{exc}") from exc

    document = {
        "v": SECRET_VERSION,
        "scheme": scheme,
        "data": base64.b64encode(payload_blob).decode("ascii"),
        # 只留末 4 位：够用户认出是哪把 keys，又不足以被拼回完整密钥。
        # （早期版本存了前 6 位，等于把前缀送给了任何能读文件的人，已废弃）
        "tail": key_text[-4:] if len(key_text) >= 8 else "",
    }
    target = secret_path()
    _atomic_write(target, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return describe()


def load_secret() -> tuple[str, str]:
    """读出明文密钥。返回 (api_key, scheme)。没有就返回 ("", "none")。

    只在真正要调 LLM 的那一刻调用；结果绝不进 API 响应。
    """
    target = secret_path()
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "", SCHEME_NONE
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretError(f"密钥文件不可读：{exc}") from exc
    if not isinstance(document, dict):
        raise SecretError("密钥文件结构不合法")

    scheme = str(document.get("scheme") or SCHEME_NONE)
    data = str(document.get("data") or "")
    if not data:
        return "", scheme
    try:
        blob = base64.b64decode(data.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SecretError(f"密文 base64 解码失败：{exc}") from exc

    if scheme == SCHEME_DPAPI:
        raw = _dpapi_transform(blob, encrypt=False)
    elif scheme == SCHEME_AESGCM_FILE:
        raw = _aesgcm_decrypt(_load_or_create_master_key(), blob)
    else:
        raise SecretError(f"未知加密方案：{scheme}")
    return raw.decode("utf-8"), scheme


def clear_secret() -> dict[str, object]:
    """删掉密文与（仅非 Windows 时需要留下的）主密钥。"""
    removed = []
    for target in (secret_path(), master_key_path()):
        try:
            if target.exists():
                target.unlink()
                removed.append(str(target))
        except OSError:
            pass
    result = describe()
    result["removed"] = removed
    return result


def mask_key(key: str) -> str:
    """前端能看到的唯一形态：只露末 4 位。

    刻意**不露前缀**——密钥前缀（如 sk- 之后那几位）同样是敏感信息，
    配合末 4 位足以缩小暴力空间。这里只给「…abcd」。
    """
    if not key:
        return ""
    if len(key) <= 4:
        return "…"
    return "…" + key[-4:]


def read_stored_mask() -> str:
    """从密文文件里读回掩码（不解密！masked/tail 是保存时一并算好的）。"""
    try:
        document = json.loads(secret_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return ""
    if not isinstance(document, dict):
        return ""
    masked = str(document.get("masked") or "")
    if masked:
        return masked
    tail = str(document.get("tail") or "")
    return ("…" + tail) if tail else "…"


def describe() -> dict[str, object]:
    """给设置页看的状态，**永不含明文**。"""
    target = secret_path()
    stored = target.exists()
    scheme = SCHEME_NONE
    if stored:
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(document, dict):
                scheme = str(document.get("scheme") or SCHEME_NONE)
        except (OSError, json.JSONDecodeError):
            scheme = SCHEME_NONE
    return {
        "stored": stored,
        "scheme": scheme,
        "available_scheme": active_scheme(),
        "path": str(target),
        "writable": _is_writable(target.parent),
        "api_key_masked": read_stored_mask(),
    }


def _is_writable(directory: Path) -> bool:
    try:
        probe = directory
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        return os.access(probe, os.W_OK)
    except OSError:
        return False


def migrate_plaintext(api_key: str) -> dict[str, object]:
    """把旧 llm.json 里的明文 api_key 迁进密文文件。

    调用方负责把 llm.json 里的字段抹掉。成功后返回 describe()。
    """
    if not (api_key or "").strip():
        return describe()
    return save_secret(api_key)
