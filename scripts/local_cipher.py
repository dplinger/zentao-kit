"""本地账密密文：标准库 + 操作系统凭据保护优先，零第三方依赖。

密钥存哪（按优先级，失败才降一档）：

1. **操作系统凭据保护**——Windows 用 DPAPI（凭据绑定当前登录用户），macOS 用登录钥匙串；
   密钥由系统代管，拷到别的机器或别的用户都解不开。
2. **个人状态目录内的密钥文件**（唯一一档降级，`login.key`，600 权限）——系统凭据保护不可用时使用。

不引入第三方包：Windows 走 `ctypes` 调用系统 DLL，macOS 走系统自带 `security` 命令。

密文构造（无论密钥存哪里都一样）：明文与 HKDF-Expand（RFC 5869）派生的密钥流逐字节异或，
再附 HMAC-SHA256 认证标签，校验后解密（Encrypt-then-MAC）；每次加密使用新的随机 salt，
同一明文每次密文都不同。解不开就明确失败，绝不把损坏或伪造的数据当明文返回。

威胁模型（如实说明，不夸大）：密钥在系统凭据保护下时，取得状态目录本身不足以解密；
降级到文件时，密钥与密文同目录，能读取该目录的程序或使用者即可解密。两种情况都挡不住
"已在本机以当前用户身份运行"的对手。
"""

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import subprocess
import sys

from common import private_write, state_dir

KEY_FILE = "login.key"
KEY_BYTES = 32              # HMAC-SHA256 密钥长度
SALT_BYTES = 16
TAG_BYTES = 32              # HMAC-SHA256 输出长度
PREFIX = "zl1."             # 密文格式标记；换算法时改这里即可识别旧密文
DPAPI_PREFIX = "dpapi."     # 密钥文件内容前缀：DPAPI 保护的密钥
PLAIN_PREFIX = "plain."     # 密钥文件内容前缀：明文密钥（降级档）
KEYCHAIN_SERVICE = "zentao-worklog"
COMMAND_TIMEOUT = 10


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(text):
    """解码 base64；编码非法时返回 None。"""
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, binascii.Error):
        return None


def _dpapi(data, protect=True):
    """调用 Windows DPAPI，用当前用户凭据保护或还原数据；失败抛 OSError。"""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def wrap(payload):
        # 保留 buffer 引用：DATA_BLOB 只存指针，buffer 被回收会读到野内存。
        buffer = ctypes.create_string_buffer(payload, len(payload))
        return Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    source, keep = wrap(data)
    out = Blob()
    crypt32 = ctypes.windll.crypt32
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not call(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def _keychain_get():
    """读取 macOS 登录钥匙串中的密钥；无条目或不可用返回 None。"""
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                                 "-a", KEYCHAIN_SERVICE, "-w"],
                                capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = _unb64(result.stdout.strip())
    return raw if raw and len(raw) == KEY_BYTES else None


def _keychain_store(key):
    """把密钥写入 macOS 登录钥匙串；失败返回 False。"""
    try:
        result = subprocess.run(["security", "add-generic-password", "-U",
                                 "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_SERVICE, "-w", _b64(key)],
                                capture_output=True, timeout=COMMAND_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _key_file_text():
    """读取密钥文件内容；不存在或不可读返回 None。"""
    path = state_dir() / KEY_FILE
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _read_key():
    """读取已保存的本机密钥；不存在或不可用返回 None（不新建）。"""
    if sys.platform == "darwin":
        key = _keychain_get()
        if key:
            return key
    text = _key_file_text()
    if not text:
        return None
    if text.startswith(DPAPI_PREFIX):
        blob = _unb64(text[len(DPAPI_PREFIX):])
        if not blob:
            return None
        try:
            return _dpapi(blob, protect=False)
        except OSError:
            return None
    return _valid(_unb64(text[len(PLAIN_PREFIX):] if text.startswith(PLAIN_PREFIX) else text))


def _store_key(key):
    """保存本机密钥：优先操作系统凭据保护，只有失败才落到状态目录内的文件。"""
    if os.name == "nt":
        try:
            blob = _dpapi(key)
        except OSError:
            blob = None
        if blob:
            private_write(state_dir() / KEY_FILE, DPAPI_PREFIX + _b64(blob))
            return
    if sys.platform == "darwin" and _keychain_store(key):
        # 钥匙串已受托保存，不另留密钥文件。
        (state_dir() / KEY_FILE).unlink(missing_ok=True)
        return
    private_write(state_dir() / KEY_FILE, PLAIN_PREFIX + _b64(key))


def _valid(raw):
    return raw if raw is not None and len(raw) == KEY_BYTES else None


def protection():
    """返回密钥当前的实际保存方式，供登录提示与排查使用。"""
    if sys.platform == "darwin" and _keychain_get():
        return "macos-keychain"
    text = _key_file_text() or ""
    if text.startswith(DPAPI_PREFIX):
        return "windows-dpapi"
    if text.startswith(PLAIN_PREFIX):
        return "file"
    return "unknown"


def _key(create=True):
    """取得本机密钥；已有就用已有的，读取失败时按 create 决定新建或放弃。"""
    key = _read_key()
    if key:
        return key
    if not create:
        return None
    key = secrets.token_bytes(KEY_BYTES)
    _store_key(key)
    return key


def _stream(key, salt, length):
    """HKDF-Expand（RFC 5869）派生指定长度的密钥流。"""
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(key, block + salt + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def encrypt(plaintext):
    """加密并编码为可放进 JSON 的字符串。"""
    key = _key()
    salt = secrets.token_bytes(SALT_BYTES)
    body = bytes(a ^ b for a, b in zip(plaintext, _stream(key, salt, len(plaintext))))
    tag = hmac.new(key, salt + body, hashlib.sha256).digest()
    return PREFIX + _b64(salt + tag + body)


def decrypt(token):
    """校验并解密；密文不可用时抛 ValueError，调用方据此要求重新登录。"""
    value = str(token or "")
    if not value.startswith(PREFIX):
        raise ValueError("密文格式不是当前版本")
    raw = _unb64(value[len(PREFIX):])
    if raw is None or len(raw) < SALT_BYTES + TAG_BYTES:
        raise ValueError("密文不可识别")
    salt, tag, body = raw[:SALT_BYTES], raw[SALT_BYTES:SALT_BYTES + TAG_BYTES], raw[SALT_BYTES + TAG_BYTES:]
    key = _key(create=False)
    if key is None or not hmac.compare_digest(tag, hmac.new(key, salt + body, hashlib.sha256).digest()):
        raise ValueError("密文校验失败")
    return bytes(a ^ b for a, b in zip(body, _stream(key, salt, len(body))))
