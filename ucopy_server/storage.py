"""
ucopy-server/storage.py
=======================

用户文件存储 + 路径校验 + 配额管理。

目录布局：
    <data_dir>/files/<username>/<rel_path>

路径校验：
    - rel_path 不能为空、不能以 / 开头、不能含 '..' 段
    - 解析后必须在 <data_dir>/files/<username>/ 下（防止 ../ 越权）
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import time
from typing import Optional, Tuple


class PathError(ValueError):
    pass


def normalize_rel_path(rel_path) -> str:
    """
    校验 + 规范化相对路径。

    合法：
        ""            -> ""     （表示用户根）
        "a/b/c.txt"   -> "a/b/c.txt"
        "subdir/"     -> "subdir"
        "  a/b  "     -> "a/b"

    非法（抛 PathError）：
        "/abs/path"   -> 绝对路径
        "a/../b"      -> 越界
        "..\\..\\x"   -> 越界

    注意：本服务器只跑在 POSIX 上（macOS / Linux）；不处理盘符。
    """
    if rel_path is None:
        return ""
    s = str(rel_path).strip()
    if not s:
        return ""
    if s.startswith("/"):
        raise PathError(f"路径不能以 '/' 开头: {rel_path!r}")
    if s.startswith("\\") or (len(s) >= 2 and s[1] == ":"):
        # Windows 风格：'C:\\foo' / '\\foo'
        raise PathError(f"路径不能含盘符或反斜杠: {rel_path!r}")
    # 去尾部 /
    s = s.rstrip("/")
    if not s:
        return ""
    # 逐段检查 ..
    parts = s.split("/")
    for p in parts:
        if p == "..":
            raise PathError(f"路径含 '..' 越权: {rel_path!r}")
    norm = "/".join(p for p in parts if p)
    # 二次归一化校验
    pos = posixpath.normpath(norm)
    if pos.startswith("../") or pos == "..":
        raise PathError(f"路径含 '..' 越权: {rel_path!r}")
    return norm


def safe_join(root: str, rel_path: str) -> str:
    """把 rel_path 安全拼到 root 下；最终路径必须在 root 之内。"""
    norm = normalize_rel_path(rel_path)
    full = os.path.realpath(os.path.join(root, norm))
    full_root = os.path.realpath(root)
    # 必须以 full_root + sep 开头（避免 prefix 攻击：root="foo"，path="foobar"）
    if not (full == full_root or full.startswith(full_root + os.sep)):
        raise PathError(f"路径越界: {rel_path}")
    return full


def user_root(data_dir: str, username: str) -> str:
    """用户的根目录绝对路径（不存在则创建）。"""
    p = os.path.join(data_dir, "files", username)
    os.makedirs(p, exist_ok=True)
    return p


def atomic_write_file(dest_path: str, src_fp, expected_size: int) -> str:
    """流式把 src_fp 写到 dest_path（先 .tmp，再 os.replace）。

    返回 sha256 hex。

    src_fp 必须支持 .read(n) —— 可以是 open() 的 file 或 io.BytesIO。
    """
    tmp = dest_path + ".tmp"
    h = hashlib.sha256()
    written = 0
    with open(tmp, "wb") as f:
        while True:
            chunk = src_fp.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            written += len(chunk)
    if expected_size and written != expected_size:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise PathError(f"上传大小不符：声明 {expected_size} 实际 {written}")
    os.replace(tmp, dest_path)
    return h.hexdigest()


def remove_path(path: str) -> None:
    """删除文件或目录（递归）；不存在返回 None。"""
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def quota_exceeded_for(conn, username: str, additional_bytes: int,
                       quota_mb: int) -> bool:
    """判断追加 additional_bytes 后是否超 quota_mb。quota_mb == 0 表示不限。"""
    import db as _db
    if quota_mb <= 0:
        return False  # 不限
    used = _db.user_total_size(conn, username)
    return (used + additional_bytes) > quota_mb * 1024 * 1024


def stat_file(path: str) -> Optional[Tuple[int, float]]:
    """返回 (size, mtime)；不存在返回 None。"""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime)
    except OSError:
        return None