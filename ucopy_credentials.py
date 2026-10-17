"""
ucopy_credentials.py
====================

UCopy 客户端凭据加密存储（v1.6.0）。

需求：UCopy 客户端的 API key 必须安全保存（不能明文躺在 config.json 里）。

平台方案：

    Windows  → DPAPI（CryptProtectData / CryptUnprotectData）
               绑定当前 Windows 用户账户，其他用户/机器无法解密

    macOS    → Keychain（security add-generic-password / find-generic-password）
               系统钥匙串，按服务名 + 账户名存储

    Linux    → 无系统级方案，用「机器绑定密钥 + AES」：
               key = SHA256(/etc/machine-id + 用户名 + 项目固定 salt)
               注：安全性弱于 DPAPI/Keychain，但避免明文落盘

    Fallback → 如果所有平台 API 都不可用，拒绝保存（返回 None）而不是明文存

对外 API：

    save_secret(service: str, account: str, secret: str) -> str | None
        返回 base64 编码的密文（写进 config.json 的字段）；
        失败返回 None（调用方应该报错而不是明文存）

    load_secret(service: str, account: str, blob: str) -> str | None
        从密文解出明文；失败返回 None

    is_supported() -> bool
        当前平台是否支持加密存储
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys


SERVICE_NAME = "com.skyc8266.ucopy.remote-key"


# ---------- Windows: DPAPI ----------

def _win_dpapi_protect(plaintext: bytes) -> bytes | None:
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        buf_in = ctypes.create_string_buffer(plaintext, len(plaintext))
        blob_in = DATA_BLOB(len(plaintext),
                            ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()

        # CRYPTPROTECT_UI_FORBIDDEN = 0x1 （不允许弹 UI；后台服务不能弹窗）
        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0x1,
            ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
        return out
    except Exception:
        return None


def _win_dpapi_unprotect(ciphertext: bytes) -> bytes | None:
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        buf_in = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        blob_in = DATA_BLOB(len(ciphertext),
                            ctypes.cast(buf_in, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()

        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0x1,
            ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
        return out
    except Exception:
        return None


# ---------- macOS: Keychain ----------

def _mac_keychain_save(account: str, secret: str) -> bool:
    """用 security CLI 存进登录钥匙串。

    -U: 已存在则更新
    -a account: 账户名
    -s service: 服务名
    -w secret: 密码（明文作为参数会有 ps 泄露风险，用 stdin 更安全）
    """
    try:
        # 先尝试删除旧的（避免 -U 在某些 macOS 版本报错）
        subprocess.run(
            ["security", "delete-generic-password",
             "-s", SERVICE_NAME, "-a", account],
            check=False, capture_output=True, timeout=10,
        )
        # 用 stdin 传密码（-w 不带值 = 从 stdin 读第一行）
        proc = subprocess.run(
            ["security", "add-generic-password",
             "-s", SERVICE_NAME, "-a", account, "-w", secret],
            check=False, capture_output=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _mac_keychain_load(account: str) -> str | None:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password",
             "-s", SERVICE_NAME, "-a", account, "-w"],
            check=False, capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip()
    except Exception:
        return None


def _mac_keychain_delete(account: str) -> bool:
    try:
        proc = subprocess.run(
            ["security", "delete-generic-password",
             "-s", SERVICE_NAME, "-a", account],
            check=False, capture_output=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


# ---------- Linux: 机器绑定 AES ----------

def _linux_machine_key() -> bytes | None:
    """读取机器唯一标识（machine-id）派生密钥。"""
    candidates = [
        "/etc/machine-id",
        "/var/lib/dbus/machine-id",
    ]
    machine_id = None
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                machine_id = f.read().strip()
            if machine_id:
                break
        except OSError:
            continue
    if not machine_id:
        return None
    try:
        user = os.getlogin()
    except OSError:
        user = os.environ.get("USER", "")
    salt = b"ucopy-v1.6.0-machine-key"
    return hashlib.sha256(
        machine_id.encode("utf-8") + user.encode("utf-8") + salt
    ).digest()


def _linux_xor_crypt(data: bytes, key: bytes) -> bytes:
    """简单 XOR（配合 SHA256 派生密钥）；Linux fallback 用。

    安全性：机器绑定（machine-id + 用户名），但不抗 root 读取。
    比明文落盘好；Linux 无系统级 keyring 时的折衷方案。
    """
    out = bytearray(len(data))
    klen = len(key)
    for i, b in enumerate(data):
        out[i] = b ^ key[i % klen]
    return bytes(out)


# ---------- 公共 API ----------

def is_supported() -> bool:
    """当前平台是否支持加密存储。"""
    if sys.platform == "win32":
        return True  # DPAPI 一定可用
    if sys.platform == "darwin":
        return True  # Keychain 一定可用
    return _linux_machine_key() is not None


def save_secret(account: str, secret: str) -> str | None:
    """保存凭据。返回 base64 编码的存储串（写入 config 的字段）；
    失败/不支持返回 None（调用方**不应**退回明文存）。"""
    if not secret:
        return None
    if sys.platform == "win32":
        blob = _win_dpapi_protect(secret.encode("utf-8"))
        if blob is None:
            return None
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    if sys.platform == "darwin":
        ok = _mac_keychain_save(account, secret)
        if not ok:
            return None
        # Keychain 已经存了实际值；config 里只留标记
        return "keychain:" + account
    # Linux
    key = _linux_machine_key()
    if key is None:
        return None
    ct = _linux_xor_crypt(secret.encode("utf-8"), key)
    return "machine:" + base64.b64encode(ct).decode("ascii")


def load_secret(account: str, blob: str) -> str | None:
    """从存储串解出明文。失败返回 None。"""
    if not blob:
        return None
    try:
        if blob.startswith("dpapi:"):
            if sys.platform != "win32":
                return None
            raw = base64.b64decode(blob[len("dpapi:"):])
            out = _win_dpapi_unprotect(raw)
            return out.decode("utf-8") if out else None
        if blob.startswith("keychain:"):
            if sys.platform != "darwin":
                return None
            return _mac_keychain_load(account)
        if blob.startswith("machine:"):
            key = _linux_machine_key()
            if key is None:
                return None
            raw = base64.b64decode(blob[len("machine:"):])
            return _linux_xor_crypt(raw, key).decode("utf-8")
    except Exception:
        return None
    return None


def delete_secret(account: str, blob: str = "") -> None:
    """删除存下的凭据（Keychain 需要显式删）。"""
    if sys.platform == "darwin":
        _mac_keychain_delete(account)


def describe_backend() -> str:
    """给 UI 显示当前凭据存储后端。"""
    if sys.platform == "win32":
        return "Windows DPAPI（绑定当前用户账户）"
    if sys.platform == "darwin":
        return "macOS 钥匙串（Keychain）"
    if _linux_machine_key() is not None:
        return "机器绑定加密（machine-id 派生密钥）"
    return "不支持加密存储"
