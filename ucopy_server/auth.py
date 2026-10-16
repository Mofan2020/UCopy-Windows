"""
ucopy-server/auth.py
====================

PBKDF2 密码哈希 + session token。

密码哈希格式：
    pbkdf2$<iterations>$<salt_hex>$<hash_hex>
例如：
    pbkdf2$200000$a1b2c3...$9f8e7d...

session token：
    - 32 字节 secrets.token_urlsafe(32)
    - 服务端只存 hash（hashlib.sha256(token)）
    - cookie HttpOnly + Secure + SameSite=Strict
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt=None,
                  iterations: int = PBKDF2_ITERATIONS) -> str:
    """返回带 scheme+iterations+salt+hash 的字符串。"""
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """constant-time 校验。"""
    if not stored or not stored.startswith("pbkdf2$"):
        return False
    parts = stored.split("$", 3)
    if len(parts) != 4:
        return False
    scheme, iter_s, salt_hex, hash_hex = parts
    if scheme != "pbkdf2":
        return False
    try:
        iterations = int(iter_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                            salt, iterations)
    return hmac.compare_digest(h, expected)


def new_session_id() -> str:
    """生成 32 字节 url-safe token（cookie 值原样返回）。"""
    return secrets.token_urlsafe(32)


def hash_session_id(session_id: str) -> str:
    """服务端只存 session_id 的 sha256，避免泄露 cookie 等于泄露会话。"""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()