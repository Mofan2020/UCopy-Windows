"""
ucopy-server/server.py
======================

UCopy 远程模式配套服务器主程序。

功能：
    - HTTPS (TLS 1.3+) with self-signed cert
    - 双栈监听 [::]:8443（IPv4 mapped 兼容）
    - REST API 路由（/api/v1/*）
    - Web GUI 静态文件 + 模板（/）
    - 用户管理（admin only）
    - macOS 自启动（launchd）管理 + Web GUI 开关
    - CLI 参数：--port / --data-dir / --bind-host / --no-autostart / --init-only

启动：
    python3 -m ucopy-server.server
或：
    python3 ucopy-server/server.py
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socketserver
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn

# 让 server.py 既能作为模块导入，也能直接 python3 server.py 跑
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(THIS_DIR)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

# PyInstaller onefile 模式下，--add-data 的 ucopy_server/static 解压到
# sys._MEIPASS/ucopy_server/static；源码运行时用 THIS_DIR/static。
# 这里解析出一份"能用"的 STATIC_DIR 路径（运行时选其一）。
def _resolve_static_dir() -> str:
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        # onefile 解压目录里的 ucopy_server/static
        candidates.append(os.path.join(sys._MEIPASS, "ucopy_server", "static"))
    candidates.append(os.path.join(THIS_DIR, "static"))
    for c in candidates:
        if os.path.isfile(os.path.join(c, "index.html")):
            return c
    # 找不到就回退到第一个候选（让 _serve_index 报 404 时消息更准确）
    return candidates[0]


STATIC_DIR = _resolve_static_dir()


def _load_static_files() -> dict:
    """把 static/ 下所有文件读到内存 dict（path -> bytes）。

    为什么不在模块加载时只算 STATIC_DIR 就够：
    PyInstaller onefile 启动时解压到 _MEIPASS/ 子目录。--detach 模式
    下 double-fork 后 macOS 系统的 TMPDIR 自动清理会 unlink 这个 _MEIPASS
    目录（即使 daemon 进程还持有 fd），导致 STATIC_DIR 字符串路径上的
    文件查找全部失败 (os.path.exists 走 lstat 拿不到 inode)。
    把所有文件读到内存后完全绕开文件系统。
    """
    result = {}
    static_root = STATIC_DIR
    if not os.path.isdir(static_root):
        return result
    for dirpath, _dirs, files in os.walk(static_root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, static_root).replace(os.sep, "/")
            try:
                with open(full, "rb") as f:
                    result[rel] = f.read()
            except OSError:
                # 文件读失败跳过（不该发生但兜底）
                pass
    return result


STATIC_FILES = _load_static_files()

try:
    from ucopy_server import db, auth, storage, launchd, cert
except ImportError:
    # 直接 python3 ucopy-server/server.py 跑（不通过包导入）
    sys.path.insert(0, THIS_DIR)
    import db  # type: ignore
    import auth  # type: ignore
    import storage  # type: ignore
    import launchd  # type: ignore
    import cert  # type: ignore

LOG = logging.getLogger("ucopy-server")

# ---------- 全局（server 启动时初始化） ----------
CFG = None
DATA_DIR = None
DB_CONN = None
SESSIONS_TTL = 86400
COOKIE_NAME = "ucopy_session"
# v1.6.0: API key 心跳锁
KEY_LOCK_TIMEOUT_SECONDS = 120  # 2 分钟没心跳就释放锁
KEY_HEARTBEAT_INTERVAL_SECONDS = 40  # 客户端发心跳间隔（参考）
KEY_MIN_SECRET_LENGTH = 16  # secret 最小长度


def init_app(cfg: dict):
    """由 main() 调用：初始化数据目录 / DB / 证书 / admin 账号。"""
    global CFG, DATA_DIR, DB_CONN, SESSIONS_TTL, COOKIE_NAME
    global KEY_LOCK_TIMEOUT_SECONDS
    CFG = cfg
    DATA_DIR = cfg["data_dir"]
    SESSIONS_TTL = int(cfg.get("session_ttl_seconds", 86400))
    COOKIE_NAME = cfg.get("cookie_name", "ucopy_session")
    KEY_LOCK_TIMEOUT_SECONDS = int(cfg.get("key_lock_timeout_seconds", 120))

    os.makedirs(DATA_DIR, exist_ok=True)
    db_path = os.path.join(DATA_DIR, "server.db")
    DB_CONN = db.open_db(db_path)

    # 初始管理员
    if db.user_count(DB_CONN) == 0:
        admin = cfg["initial_admin"]
        db.add_user(DB_CONN, admin["username"],
                    auth.hash_password(admin["password"]),
                    "admin", quota_mb=0)  # 0 = 不限
        LOG.info(f"已创建初始管理员: {admin['username']}")


# ---------- HTTP handler ----------
class UCRequestHandler(BaseHTTPRequestHandler):
    # 抑制 BaseHTTPRequestHandler 默认 stdout 日志（自定义日志格式）
    protocol_version = "HTTP/1.1"  # 支持 keep-alive；curl 等客户端行为更稳
    def log_message(self, fmt, *args):
        LOG.debug(f"{self.address_string()} - {fmt % args}")

    # ----- 工具 -----
    def _json(self, status: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-UCopy-Protocol", "1")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status: int, code: str, message: str):
        self._json(status, {"ok": False, "error": message, "code": code})

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def _cookie_session(self):
        raw = self.headers.get("Cookie", "")
        for piece in raw.split(";"):
            k, _, v = piece.strip().partition("=")
            if k == COOKIE_NAME:
                return v
        return None

    def _session_user(self):
        token = self._cookie_session()
        if not token:
            return None
        id_hash = auth.hash_session_id(token)
        return db.lookup_session(DB_CONN, id_hash)

    def _require_user(self, *roles):
        """返回 sqlite3.Row user；未登录返回 None；权限不够返回 False。"""
        user = self._session_user()
        if not user:
            return None
        if roles and user["role"] not in roles:
            return False
        return user

    def _set_session_cookie(self, token: str):
        # v1.7.0: HTTP 模式下不发 Secure flag（否则 cookie 客户端会拒绝，session 失效）
        secure = "Secure; " if not (CFG or {}).get("disable_https") else ""
        cookie = (
            f"{COOKIE_NAME}={token}; HttpOnly; {secure}SameSite=Strict; "
            f"Path=/; Max-Age={SESSIONS_TTL}"
        )
        self.send_header("Set-Cookie", cookie)

    def _clear_session_cookie(self):
        secure = "Secure; " if not (CFG or {}).get("disable_https") else ""
        cookie = f"{COOKIE_NAME}=deleted; HttpOnly; {secure}Path=/; Max-Age=0"
        self.send_header("Set-Cookie", cookie)

    # ----- 路由分发 -----
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/"):
            self._dispatch_api("GET")
        else:
            self._serve_static_or_index()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/"):
            self._dispatch_api("POST")
        else:
            self._err(404, "not_found", f"no route: {self.command} {path}")

    def do_PUT(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/"):
            self._dispatch_api("PUT")
        else:
            self._err(404, "not_found", f"no route: {self.command} {path}")

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/"):
            self._dispatch_api("DELETE")
        else:
            self._err(404, "not_found", f"no route: {self.command} {path}")

    def do_PATCH(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/"):
            self._dispatch_api("PATCH")
        else:
            self._err(404, "not_found", f"no route: {self.command} {path}")

    # ----- API 路由 -----
    def _dispatch_api(self, method: str):
        path = urllib.parse.urlsplit(self.path).path
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        qs = {k: v[0] if v else None for k, v in q.items()}

        try:
            if path == "/api/v1/ping" and method == "GET":
                return self._json(200, {"ok": True, "version": "1"})
            if path == "/api/v1/login" and method == "POST":
                return self._api_login()
            if path == "/api/v1/logout" and method == "POST":
                return self._api_logout()
            if path == "/api/v1/me" and method == "GET":
                return self._api_me()
            if path == "/api/v1/config" and method == "GET":
                return self._api_get_config()
            if path == "/api/v1/config" and method == "PUT":
                return self._api_put_config()

            if path == "/api/v1/files" and method == "GET":
                return self._api_list(qs)
            if path == "/api/v1/files/download" and method == "GET":
                return self._api_download(qs)
            if path == "/api/v1/files/check" and method == "GET":
                return self._api_check(qs)
            if path == "/api/v1/files/upload" and method == "POST":
                return self._api_upload(qs)
            if path == "/api/v1/files" and method == "DELETE":
                return self._api_delete_file(qs)

            if path == "/api/v1/admin/users" and method == "GET":
                return self._api_admin_list_users()
            if path == "/api/v1/admin/users" and method == "POST":
                return self._api_admin_create_user()
            if path.startswith("/api/v1/admin/users/") and method == "PATCH":
                username = urllib.parse.unquote(path[len("/api/v1/admin/users/"):])
                return self._api_admin_update_user(username)
            if path.startswith("/api/v1/admin/users/") and method == "DELETE":
                username = urllib.parse.unquote(path[len("/api/v1/admin/users/"):])
                return self._api_admin_delete_user(username)

            # 修改自己的密码（任何登录用户）
            if path == "/api/v1/users/me/password" and method == "PUT":
                return self._api_change_my_password()

            if path == "/api/v1/launchd" and method == "GET":
                return self._api_launchd_status()
            if path == "/api/v1/launchd/install" and method == "POST":
                return self._api_launchd_install()
            if path == "/api/v1/launchd/uninstall" and method == "POST":
                return self._api_launchd_uninstall()

            # v1.7.0: 强制 HTTP 模式开关 + 服务重启
            if path == "/api/v1/system" and method == "GET":
                return self._api_system_get()
            if path == "/api/v1/system" and method == "PUT":
                return self._api_system_update()
            if path == "/api/v1/system/restart" and method == "POST":
                return self._api_system_restart()

            # v1.6.0: API key 管理（需 user session 登录）
            if path == "/api/v1/keys" and method == "GET":
                return self._api_keys_list()
            if path == "/api/v1/keys" and method == "POST":
                return self._api_keys_create()
            # 具体的 key 操作：/api/v1/keys/{id} 和 /api/v1/keys/{id}/release
            if path.startswith("/api/v1/keys/") and method == "DELETE":
                rest = path[len("/api/v1/keys/"):]
                # 区分 /api/v1/keys/{id} /api/v1/keys/{id}/release
                if "/" in rest and rest.endswith("/release"):
                    key_id = int(rest[:-len("/release")])
                    return self._api_keys_release(key_id)
                key_id = int(rest)
                return self._api_keys_delete(key_id)

            # v1.6.0: 用 API key 登录 + 心跳（不需要 user session）
            if path == "/api/v1/auth/key" and method == "POST":
                return self._api_auth_with_key()
            if path == "/api/v1/auth/heartbeat" and method == "POST":
                return self._api_auth_heartbeat()

            return self._err(404, "not_found", f"no api: {method} {path}")
        except storage.PathError as e:
            return self._err(400, "invalid_path", str(e))
        except Exception as e:
            LOG.exception(f"API 异常 {method} {path}: {e}")
            return self._err(500, "internal", str(e))

    # ----- API 实现 -----
    def _api_login(self):
        body = self._read_json()
        if body is None:
            return self._err(400, "bad_request", "invalid json")
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or not password:
            return self._err(400, "bad_request", "username/password required")
        user = db.get_user(DB_CONN, username)
        if not user or not auth.verify_password(password, user["password_hash"]):
            return self._err(401, "unauthenticated", "invalid credentials")
        # 建 session
        token = auth.new_session_id()
        db.add_session(DB_CONN, auth.hash_session_id(token),
                       username, SESSIONS_TTL)
        self.send_response(200)
        self._set_session_cookie(token)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-UCopy-Protocol", "1")
        self.send_header("Connection", "close")
        body = json.dumps({"ok": True, "username": username,
                           "role": user["role"]}).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_logout(self):
        token = self._cookie_session()
        if token:
            db.delete_session(DB_CONN, auth.hash_session_id(token))
        self.send_response(200)
        self._clear_session_cookie()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Connection", "close")
        body = b'{"ok": true}'
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api_me(self):
        user = self._session_user()
        if not user:
            return self._err(401, "unauthenticated", "未登录")
        used = db.user_total_size(DB_CONN, user["username"])
        quota_mb = user["quota_mb"]
        return self._json(200, {
            "ok": True,
            "username": user["username"],
            "role": user["role"],
            "quota_mb": quota_mb,
            "used_bytes": used,
        })

    def _api_get_config(self):
        user = self._session_user()
        if not user:
            return self._err(401, "unauthenticated", "未登录")
        cfg = db.get_user_config(DB_CONN, user["username"])
        if not cfg:
            return self._json(200, {
                "ok": True,
                "config": {},
                "version": 0,
                "last_modified": "",
            })
        return self._json(200, {"ok": True, **cfg})

    def _api_put_config(self):
        user = self._session_user()
        if not user:
            return self._err(401, "unauthenticated", "未登录")
        body = self._read_json()
        if body is None or "config" not in body:
            return self._err(400, "bad_request", "需要 config 字段")
        info = db.put_user_config(DB_CONN, user["username"], body["config"])
        return self._json(200, {"ok": True, **info})

    def _api_list(self, qs):
        user = self._require_user("admin", "user", "readonly")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        rel = storage.normalize_rel_path(qs.get("path") or "")
        uroot = storage.user_root(DATA_DIR, cfg_username(user))
        files = db.list_files(DB_CONN, cfg_username(user), rel)
        dirs = db.list_dirs(DB_CONN, cfg_username(user), rel)
        # 对每个目录尝试拿 mtime（用目录里最早的文件 mtime 或 0）
        out_entries = []
        for d in dirs:
            name = d["name"]
            d_full = storage.safe_join(uroot, os.path.join(rel, name) if rel else name)
            try:
                mt = os.stat(d_full).st_mtime
            except OSError:
                mt = 0
            out_entries.append({"name": name, "type": "dir",
                                "size": None, "mtime": mt, "sha256": None})
        for f in files:
            out_entries.append(f)
        out_entries.sort(key=lambda x: (x["type"] != "dir", x["name"]))
        return self._json(200, {"ok": True, "entries": out_entries,
                                "path": rel})

    def _api_download(self, qs):
        user = self._require_user("admin", "user", "readonly")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        rel = storage.normalize_rel_path(qs.get("path") or "")
        if not rel:
            return self._err(400, "bad_request", "path 必须指向文件")
        uroot = storage.user_root(DATA_DIR, cfg_username(user))
        full = storage.safe_join(uroot, rel)
        if not os.path.isfile(full):
            return self._err(404, "not_found", "文件不存在")
        size = os.path.getsize(full)
        name = os.path.basename(rel)
        try:
            with open(full, "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name}"')
                self.send_header("Connection", "close")
                self.end_headers()
                shutil.copyfileobj(f, self.wfile, 64 * 1024)
        except OSError as e:
            LOG.warning(f"下载失败 {rel}: {e}")
        return None

    def _api_check(self, qs):
        user = self._require_user("admin", "user")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        rel = storage.normalize_rel_path(qs.get("path") or "")
        sha = (qs.get("sha256") or "").lower()
        if not rel or not sha:
            return self._err(400, "bad_request", "path/sha256 必填")
        row = db.get_file_meta(DB_CONN, cfg_username(user), rel)
        if row and row["sha256"] == sha:
            return self._json(200, {"ok": True, "match": True,
                                    "size": row["size"], "mtime": row["mtime"]})
        return self._json(404, {"ok": True, "match": False})

    def _api_upload(self, qs):
        user = self._require_user("admin", "user")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        rel = storage.normalize_rel_path(qs.get("path") or "")
        if not rel:
            return self._err(400, "bad_request", "path 必须指向文件")
        uroot = storage.user_root(DATA_DIR, cfg_username(user))
        full = storage.safe_join(uroot, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)

        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._err(400, "bad_request",
                             "需要 multipart/form-data 上传")

        # 解析 multipart
        try:
            fields, file_iter = self._parse_multipart(ctype)
        except ValueError as e:
            return self._err(400, "bad_request", f"multipart 解析失败: {e}")

        sha = fields.get("sha256", "").lower()
        try:
            size_decl = int(fields.get("size") or 0)
        except ValueError:
            return self._err(400, "bad_request", "size 必须为整数")
        try:
            mtime_decl = float(fields.get("mtime") or 0)
        except ValueError:
            mtime_decl = 0.0

        if not sha:
            return self._err(400, "bad_request", "sha256 必填")

        # quota 检查（粗算：用声明 size；最终以真实写盘为准）
        quota_mb = user["quota_mb"] if user["quota_mb"] > 0 else 0
        if storage.quota_exceeded_for(
            DB_CONN, cfg_username(user), size_decl, quota_mb
        ):
            return self._err(413, "quota_exceeded", "配额不足")

        # 检查 sha256 是否与现有文件一致
        row = db.get_file_meta(DB_CONN, cfg_username(user), rel)
        if row and row["sha256"] == sha and size_decl == row["size"]:
            # 内容相同，无需重写（但仍要消费 body 防止连接重置）
            for _ in file_iter:
                pass
            return self._json(200, {"ok": True, "unchanged": True,
                                    "size": row["size"], "sha256": row["sha256"]})

        # 写盘
        actual_sha = storage.atomic_write_file(full, file_iter, size_decl)
        if actual_sha != sha:
            try:
                os.remove(full)
            except OSError:
                pass
            return self._err(400, "size_mismatch",
                             f"sha256 不一致（声明 {sha[:12]}… 实际 {actual_sha[:12]}…）")

        # 设置 mtime
        try:
            os.utime(full, (mtime_decl, mtime_decl))
        except OSError:
            pass

        size_real = os.path.getsize(full)
        db.upsert_file(DB_CONN, cfg_username(user), rel,
                       size=size_real, mtime=mtime_decl, sha256=actual_sha)
        return self._json(200, {"ok": True, "unchanged": False,
                                "size": size_real, "sha256": actual_sha})

    def _api_delete_file(self, qs):
        user = self._require_user("admin", "user")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        rel = storage.normalize_rel_path(qs.get("path") or "")
        if not rel:
            return self._err(400, "bad_request", "path 必填")
        uroot = storage.user_root(DATA_DIR, cfg_username(user))
        full = storage.safe_join(uroot, rel)
        storage.remove_path(full)
        db.delete_file_index(DB_CONN, cfg_username(user), rel)
        return self._json(200, {"ok": True})

    # ----- admin -----
    def _api_admin_list_users(self):
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        users = []
        for u in db.list_users(DB_CONN):
            used = db.user_total_size(DB_CONN, u["username"])
            users.append({
                "username": u["username"],
                "role": u["role"],
                "quota_mb": u["quota_mb"],
                "used_bytes": used,
                "created_at": u["created_at"],
            })
        return self._json(200, {"ok": True, "users": users})

    def _api_admin_create_user(self):
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        body = self._read_json()
        if body is None:
            return self._err(400, "bad_request", "invalid json")
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        role = body.get("role") or "user"
        quota_mb = int(body.get("quota_mb") or 10240)
        if not username or not password:
            return self._err(400, "bad_request", "username/password 必填")
        if db.get_user(DB_CONN, username):
            return self._err(409, "user_exists", "用户已存在")
        try:
            db.add_user(DB_CONN, username, auth.hash_password(password),
                        role, quota_mb)
        except ValueError as e:
            return self._err(400, "bad_request", str(e))
        # 创建根目录
        storage.user_root(DATA_DIR, username)
        return self._json(200, {"ok": True})

    def _api_admin_update_user(self, username):
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        body = self._read_json() or {}
        kwargs = {}
        if "password" in body:
            kwargs["password_hash"] = auth.hash_password(body["password"])
        if "role" in body:
            kwargs["role"] = body["role"]
        if "quota_mb" in body:
            kwargs["quota_mb"] = int(body["quota_mb"])
        try:
            db.update_user(DB_CONN, username, **kwargs)
        except ValueError as e:
            return self._err(400, "bad_request", str(e))
        return self._json(200, {"ok": True})

    def _api_admin_delete_user(self, username):
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        # 不允许删自己
        if user["username"] == username:
            return self._err(400, "bad_request", "不能删除当前登录的 admin")
        # 删文件
        uroot = os.path.join(DATA_DIR, "files", username)
        storage.remove_path(uroot)
        db.delete_user(DB_CONN, username)
        return self._json(200, {"ok": True})

    # ----- 修改自己的密码 -----
    def _api_change_my_password(self):
        """任何登录用户改自己密码。需提供 old_password 验证。"""
        user = self._require_user()
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        body = self._read_json() or {}
        old = body.get("old_password", "")
        new = body.get("new_password", "")
        if not old or not new:
            return self._err(400, "bad_request", "old_password / new_password 必填")
        if len(new) < 6:
            return self._err(400, "bad_request", "新密码至少 6 个字符")
        # 校验旧密码
        row = db.get_user(DB_CONN, user["username"])
        if not row or not auth.verify_password(old, row["password_hash"]):
            return self._err(403, "forbidden", "旧密码错误")
        try:
            db.update_user(DB_CONN, user["username"],
                           password_hash=auth.hash_password(new))
        except ValueError as e:
            return self._err(400, "bad_request", str(e))
        # 改完密码后让当前 session 失效（强制重新登录）
        token = self._cookie_session()
        if token:
            db.delete_session(DB_CONN, auth.hash_session_id(token))
        # 构造响应并附带清 cookie 头
        body_json = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body_json)))
        self.send_header("X-UCopy-Protocol", "1")
        self.send_header("Connection", "close")
        # 清 cookie（必须在 send_response 之后）
        self.send_header("Set-Cookie",
            f"{COOKIE_NAME}=deleted; HttpOnly; Secure; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body_json)
        return None

    # ----- API keys (v1.6.0) -----
    def _api_keys_list(self):
        """列出当前用户的 keys（不含 secret/token）。"""
        user = self._require_user()
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        # 顺手清 stale 锁
        db.purge_stale_api_key_locks(DB_CONN, KEY_LOCK_TIMEOUT_SECONDS)
        rows = db.list_api_keys(DB_CONN, user["username"])
        now = time.time()
        keys = []
        for r in rows:
            locked = bool(r.get("current_client_id"))
            hb = r.get("last_heartbeat_at")
            keys.append({
                "id": r["id"],
                "name": r["name"],
                "created_at": r["created_at"],
                "current_client_id": r.get("current_client_id"),
                "last_heartbeat_at": hb,
                "heartbeat_age_seconds": (now - hb) if hb else None,
                "in_use": locked,
            })
        return self._json(200, {"ok": True, "keys": keys})

    def _api_keys_create(self):
        """创建 key：返回 key_token + key_secret（只此一次）。"""
        user = self._require_user()
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        body = self._read_json() or {}
        name = (body.get("name") or "").strip()
        if not name:
            return self._err(400, "bad_request", "name 必填")
        if len(name) > 64:
            return self._err(400, "bad_request", "name 太长（最多 64 字符）")
        # 生成 token + secret
        key_token = auth.new_key_token()
        key_secret = auth.new_key_token()  # 同一 API 复用（32 字节 url-safe）
        secret_hash, secret_salt = auth.hash_key_secret(key_secret)
        key_id = db.add_api_key(
            DB_CONN,
            username=user["username"],
            token_hash=auth.hash_key_token(key_token),
            secret_hash=secret_hash,
            secret_salt=secret_salt,
            name=name,
        )
        LOG.info(f"[keys] {user['username']} 创建 key #{key_id} ({name})")
        return self._json(200, {
            "ok": True,
            "id": key_id,
            "name": name,
            # 客户端填的完整 key = token + "." + secret
            "key": f"{key_token}.{key_secret}",
            "note": "请立即保存。服务器只存 hash，无法再次显示。",
        })

    def _api_keys_delete(self, key_id: int):
        """删除 key（只能删自己的）。"""
        user = self._require_user()
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        n = db.delete_api_key(DB_CONN, key_id, user["username"])
        if n == 0:
            return self._err(404, "not_found", "key 不存在或不属于当前用户")
        LOG.info(f"[keys] {user['username']} 删除 key #{key_id}")
        return self._json(200, {"ok": True})

    def _api_keys_release(self, key_id: int):
        """强制释放 key 的独占锁（踢掉当前占用的客户端）。

        用于场景：客户端崩了没来得及释放锁，但 2 分钟心跳超时也会自动释放。
        """
        user = self._require_user()
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "权限不足")
        row = db.get_api_key_by_id(DB_CONN, key_id, user["username"])
        if not row:
            return self._err(404, "not_found", "key 不存在或不属于当前用户")
        if not row["current_client_id"]:
            return self._json(200, {"ok": True, "released": False,
                                    "message": "该 key 当前未被占用"})
        prev_client = row["current_client_id"]
        db.release_api_key_lock(DB_CONN, key_id, user["username"], prev_client)
        LOG.info(f"[keys] {user['username']} 强制释放 key #{key_id}（踢断 {prev_client}）")
        return self._json(200, {"ok": True, "released": True,
                                "previous_client_id": prev_client})

    def _api_auth_with_key(self):
        """用 API key 登录（UCopy 客户端）。

        body: {key, client_id}
        - key 格式：<token>.<secret>
        - 校验 token hash，再校验 secret（pbkdf2）
        - 获取独占锁（如果已有 current_client_id 则抢占 = 踢掉旧客户端）
        - 返回 user / role / quota_mb + key_id
        """
        db.purge_stale_api_key_locks(DB_CONN, KEY_LOCK_TIMEOUT_SECONDS)
        body = self._read_json() or {}
        full_key = (body.get("key") or "").strip()
        client_id = (body.get("client_id") or "").strip()
        if not full_key or "." not in full_key:
            return self._err(400, "bad_request", "key 格式错误（应为 <token>.<secret>）")
        if not client_id:
            return self._err(400, "bad_request", "client_id 必填")
        if len(client_id) > 128:
            return self._err(400, "bad_request", "client_id 太长")
        token, _, secret = full_key.partition(".")
        token_hash = auth.hash_key_token(token)
        row = db.get_api_key_by_token(DB_CONN, token_hash)
        if not row:
            return self._err(401, "unauthenticated", "无效的 key")
        if not auth.verify_key_secret(secret, row["secret_hash"]):
            return self._err(401, "unauthenticated", "无效的 key")
        # 用户可能已被删除
        user = db.get_user(DB_CONN, row["username"])
        if not user:
            return self._err(401, "unauthenticated", "对应账号已删除")
        # 独占锁：抢占式（兼容"客户端重装/换机"场景）
        prev = row["current_client_id"]
        db.claim_api_key_lock(DB_CONN, row["id"], row["username"], client_id)
        if prev and prev != client_id:
            LOG.info(f"[keys] key #{row['id']} 被 {client_id} 接管（原 {prev} 被踢）")
        # v1.6.0: key 认证也发 session cookie（业务 API 复用 session 认证）
        session_token = auth.new_session_id()
        db.add_session(DB_CONN, auth.hash_session_id(session_token),
                       row["username"], SESSIONS_TTL)
        LOG.info(f"[keys] {row['username']} 用 key #{row['id']} 登录 (client_id={client_id})")
        # 需要带 Set-Cookie，所以手写响应（不能用 _json）
        self.send_response(200)
        self._set_session_cookie(session_token)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-UCopy-Protocol", "1")
        self.send_header("Connection", "close")
        body_json = json.dumps({
            "ok": True,
            "username": row["username"],
            "role": user["role"],
            "quota_mb": user["quota_mb"],
            "key_id": row["id"],
            "client_id": client_id,
            "previous_client_id": prev if prev != client_id else None,
            "heartbeat_interval_seconds": KEY_HEARTBEAT_INTERVAL_SECONDS,
            "lock_timeout_seconds": KEY_LOCK_TIMEOUT_SECONDS,
        }, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body_json)))
        self.end_headers()
        self.wfile.write(body_json)
        return None

    def _api_auth_heartbeat(self):
        """客户端心跳：保持独占锁。

        body: {key, client_id}
        - key 校验同 _api_auth_with_key
        - 如果 client_id 不匹配（被其他客户端接管）→ 410 gone
        """
        db.purge_stale_api_key_locks(DB_CONN, KEY_LOCK_TIMEOUT_SECONDS)
        body = self._read_json() or {}
        full_key = (body.get("key") or "").strip()
        client_id = (body.get("client_id") or "").strip()
        if not full_key or "." not in full_key:
            return self._err(400, "bad_request", "key 格式错误")
        if not client_id:
            return self._err(400, "bad_request", "client_id 必填")
        token, _, secret = full_key.partition(".")
        row = db.get_api_key_by_token(DB_CONN, auth.hash_key_token(token))
        if not row or not auth.verify_key_secret(secret, row["secret_hash"]):
            return self._err(401, "unauthenticated", "无效的 key")
        ok = db.update_api_key_heartbeat(
            DB_CONN, row["id"], row["username"], client_id)
        if not ok:
            return self._err(410, "key_lock_lost",
                             "该 key 已被其他客户端接管，请重新连接")
        return self._json(200, {"ok": True,
                                "heartbeat_interval_seconds": KEY_HEARTBEAT_INTERVAL_SECONDS})

    # ----- launchd -----
    def _api_launchd_status(self):
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        return self._json(200, {
            "ok": True,
            "platform": _platform(),
            "installed": launchd.is_installed(),
            "status": launchd.get_status(),
            "plist_path": launchd.plist_path(),
        })

    def _api_launchd_install(self):
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        if launchd.is_installed():
            return self._err(409, "already_installed", "已经安装过")
        # 拼出自启动命令：复用当前 python + 当前 server.py 路径
        # v1.7.0: 强制带 --detach（用户硬性偏好："服务程序无论是...当前启动为launchd自启动，
        # 都使用detach模式（无法指定为其他）"）。同时把当前模式（端口 + disable_https）一起带上。
        cfg = get_cfg()
        data_dir = cfg.get("data_dir") or os.path.join(
            os.path.expanduser("~"), "UCopyServer")
        prog_args = [
            sys.executable,
            os.path.abspath(__file__),
            "--data-dir", data_dir,
            "--detach",
        ]
        if cfg.get("port"):
            prog_args.extend(["--port", str(int(cfg["port"]))])
        if cfg.get("bind_host"):
            prog_args.extend(["--bind-host", cfg["bind_host"]])
        if cfg.get("disable_https"):
            prog_args.append("--disable-https")
        ok = launchd.install(prog_args)
        return self._json(200 if ok else 500, {
            "ok": ok,
            "installed": launchd.is_installed(),
            "status": launchd.get_status(),
        })

    def _api_launchd_uninstall(self):
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        ok = launchd.uninstall()
        return self._json(200, {"ok": ok,
                                "installed": launchd.is_installed(),
                                "status": launchd.get_status()})

    # ----- v1.7.0: system (HTTP/HTTPS 模式 + restart) -----
    def _api_system_get(self):
        """返回服务器当前模式（http/https 状态、port、是否 launchd 管控）。"""
        user = self._require_user()
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        return self._json(200, {
            "ok": True,
            "scheme": "http" if CFG.get("disable_https") else "https",
            "disable_https": bool(CFG.get("disable_https")),
            "port": int(CFG.get("port") or 8443),
            "bind_host": CFG.get("bind_host", "::"),
            "managed_by_launchd": bool(launchd.is_installed()) if sys.platform == "darwin" else False,
            # 重启后实际生效要靠 kill 自身 + detach 重启 / launchctl kickstart
            "restart_supported": True,
        })

    def _api_system_update(self):
        """修改 disable_https。**必须调用 /api/v1/system/restart 才生效**。"""
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        body = self._read_json() or {}
        if "disable_https" not in body:
            return self._err(400, "bad_request", "缺少 disable_https 字段")
        new_val = bool(body["disable_https"])
        old_val = bool(CFG.get("disable_https"))
        if new_val == old_val:
            return self._json(200, {
                "ok": True,
                "disable_https": old_val,
                "changed": False,
                "restart_required": False,
            })
        CFG["disable_https"] = new_val
        from ucopy_server import config as _server_config
        _server_config.save(CFG)
        # 同步 launchd plist（让 launchd 启动时用上正确模式）
        if sys.platform == "darwin" and launchd.is_installed():
            try:
                import plistlib as _pl
                plist_path = launchd.plist_path()
                plist = _pl.load(open(plist_path, "rb"))
                # 保留原 ProgramArguments 形式（v1.6.x 是 [/bin/sh, -c, "<exec>"]），
                # 只追加 / 移除 --disable-https 标志，不要把 list 形式直接覆盖上去
                # —— 那会把旧的 /bin/sh 形式（兼容老 plist）破坏掉
                args = list(plist.get("ProgramArguments", []))
                if new_val and "--disable-https" not in args:
                    args.append("--disable-https")
                elif not new_val:
                    args = [a for a in args if a != "--disable-https"]
                plist["ProgramArguments"] = args
                with open(plist_path, "wb") as f:
                    _pl.dump(plist, f)
                LOG.info(f"[system] launchd plist 同步 disable_https={new_val}")
            except Exception as e:
                LOG.warning(f"[system] 同步 launchd plist 失败: {e}")
        return self._json(200, {
            "ok": True,
            "disable_https": new_val,
            "changed": True,
            "restart_required": True,
            "next_url": f"{'http' if new_val else 'https'}://localhost:{CFG['port']}/",
        })

    def _api_system_restart(self):
        """重启服务（自杀 + 自动重启）。

        策略：
          - 当前进程是 launchd 启动的（PPID=1 且 $LAUNCHED_BY_LAUNCHD=1）
            → launchctl kickstart -k（launchd 会重读 plist + 重启进程）
          - 其他情况 → 当前进程 os._exit(0) 立即终止（不调 httpd.shutdown()，避免在
            serve_forever 线程里调 shutdown 死锁）；服务依赖外部 --detach 重启或
            启动 launchd / 用户手动
        """
        import subprocess as _sp
        user = self._require_user("admin")
        if user is None:
            return self._err(401, "unauthenticated", "未登录")
        if user is False:
            return self._err(403, "forbidden", "需要 admin")
        # 立即返回响应（client 不阻塞）
        self._json(200, {"ok": True, "restarting": True})
        try:
            self.wfile.flush()
        except Exception:
            pass
        # 判断当前进程是否由 launchd 启动（PPID=1 + 环境变量）
        # 不用 is_installed()：plist 装了不代表当前这个进程就是它启动的
        launched_by_launchd = (
            sys.platform == "darwin"
            and os.getppid() == 1
            and os.environ.get("LAUNCHED_BY_LAUNCHD") == "1"
            and launchd.is_installed()
        )
        if launched_by_launchd:
            try:
                LOG.info("[system] launchd 管理的进程：通过 launchctl kickstart -k 重启")
                # macOS launchd target 格式必须为 gui/<uid>/<label>，只传 label 会报
                # "Unrecognized target specifier"。uid 从 os.getuid() 取。
                _target = f"gui/{os.getuid()}/{launchd.LABEL}"
                _sp.Popen(["launchctl", "kickstart", "-k", _target],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                import threading as _t
                _t.Timer(0.5, lambda: os._exit(0)).start()
                return
            except Exception as e:
                LOG.warning(f"[system] launchctl kickstart 失败: {e}，回退到 os._exit")
        # 非 launchd 启动的进程：直接 _exit
        LOG.info(f"[system] 收到 restart 请求，进程立即退出"
                 f"（launchd 启动={launched_by_launchd}；依赖外部 --detach / 手动重启）")
        import threading as _t
        _t.Timer(0.3, lambda: os._exit(0)).start()

    # ----- multipart -----
    def _parse_multipart(self, content_type: str):
        """解析 multipart/form-data。返回 (fields: dict, file_iter)。

        用 stdlib email 解析（Python 3.13 移除了 cgi 后改用这个）。
        把 body 全部读到一个临时文件里喂给 BytesParser，避免大文件全进内存。
        """
        m = re.search(r'boundary=(?:"([^"]+)"|([^;\s]+))', content_type)
        if not m:
            raise ValueError("no boundary in Content-Type")
        boundary = (m.group(1) or m.group(2))
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length <= 0:
            raise ValueError("empty body")

        # 写到临时文件
        tmp = tempfile.NamedTemporaryFile(delete=False, prefix="ucopy_upload_")
        try:
            remaining = content_length
            while remaining > 0:
                chunk = self.rfile.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                tmp.write(chunk)
                remaining -= len(chunk)
            tmp.close()

            with open(tmp.name, "rb") as f:
                body = f.read()

            # email parser 需要 Content-Type header 才会按 multipart 解析；
            # 客户端已经发了，但 body 里没带 envelope（multipart 协议在原始 HTTP
            # 报文里），所以包一层。
            from email import policy as _policy
            from email.parser import BytesParser as _BP
            envelope = (
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {content_length}\r\n\r\n"
            ).encode("ascii")
            msg = _BP(policy=_policy.default).parsebytes(envelope + body)
            fields = {}
            file_iter = None
            for part in msg.iter_parts():
                cd = part.get("Content-Disposition", "") or ""
                name_m = re.search(r'name="([^"]+)"', cd)
                filename_m = re.search(r'filename="([^"]*)"', cd)
                if not name_m:
                    continue
                name = name_m.group(1)
                payload = part.get_payload(decode=True) or b""
                if not isinstance(payload, bytes):
                    payload = b""
                if filename_m and filename_m.group(1):
                    if name == "file":
                        file_iter = io.BytesIO(payload)
                    else:
                        fields[name] = payload.decode("utf-8", errors="replace")
                else:
                    try:
                        fields[name] = payload.decode("utf-8")
                    except Exception:
                        fields[name] = payload.decode("utf-8", errors="replace")
            if file_iter is None:
                raise ValueError("multipart body 缺少 file part")
            return fields, file_iter
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass

    # ----- 静态文件 / Web GUI -----
    def _serve_static_or_index(self):
        path = urllib.parse.urlsplit(self.path).path.lstrip("/")
        # 安全：拒绝 .. / 绝对
        if ".." in path.split("/") or path.startswith("/"):
            return self._err(404, "not_found", "no such path")
        if not path:
            return self._serve_index()
        # v1.5.5: 用内存缓存避免 detach 模式下 _MEIPASS 目录被 macOS 清理
        data = STATIC_FILES.get(path)
        if data is not None:
            mime, _ = mimetypes.guess_type(path)
            mime = mime or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return
        # 没有的路径：fallback 到 index.html（前端路由）
        return self._serve_index()

    def _serve_index(self):
        # 简单的 SPA：所有非 /api/v1/* 请求都返回 index.html
        # v1.5.5: 用内存缓存避免 detach 模式下 _MEIPASS 目录被 macOS 清理
        index_data = STATIC_FILES.get("index.html")
        if not index_data:
            return self._err(404, "not_found", "Web GUI 未安装（static/index.html 不存在）")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(index_data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(index_data)


# ---------- helpers ----------
def cfg_username(user):
    """admin 看全部；其他只能看自己。这里 user_row 已经是当前 session，username
    一定等于自己的；admin 调用别的 user 需要传 username 字段（暂未实现）。"""
    return user["username"]


def _platform():
    import platform as _p
    return _p.system()


# ---------- main ----------
def make_server(cfg: dict, httpd):
    """wrap httpd with TLS（基于已有 ctx）。v1.7.0: cfg['disable_https']=True 时不加载 TLS。"""
    if cfg.get("disable_https"):
        # HTTP 模式：不加载证书，不 wrap socket
        return httpd
    cert_path, key_path = cert.ensure_cert(cfg["data_dir"])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def _server_address_family(bind_host: str):
    """根据 bind_host 决定 ThreadingHTTPServer 用 AF_INET 还是 AF_INET6。

    - '0.0.0.0' / '127.0.0.1' / IPv4 → AF_INET
    - '[::]' / '::' / 含 ':' → AF_INET6（IPv6 双栈靠 IPV6_V6ONLY=0 实现）
    """
    import socket as _socket
    if ":" in bind_host:
        return _socket.AF_INET6
    return _socket.AF_INET


def _strip_brackets(host: str) -> str:
    """剥 IPv6 字面值的方括号：[::] -> ::；裸 :: 不动。"""
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


def _enable_ipv6_dual_stack(sock):
    """给 IPv6 socket 开 IPV6_V6ONLY=0，让 :: 通配同时接受 IPv4 mapped。"""
    import socket as _socket
    if sock.family == _socket.AF_INET6 and hasattr(_socket, "IPV6_V6ONLY"):
        try:
            sock.setsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 0)
        except OSError as e:
            LOG.warning(f"[bind] 启用 IPv4-mapped over IPv6 失败: {e}")


class ThreadingHTTPSServer(ThreadingHTTPServer):
    """Python 3.7+ 的 ThreadingHTTPServer 已经线程化了，继承即可用。

    重写 __init__ 是为了在调用 socketserver.TCPServer.__init__ 之前
    先把 [::] 剥成裸 ::（macOS getaddrinfo('[::]', port) 报 Errno 8）。
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, bind_host=None):
        # 关键：socketserver 内部会对 server_address 调 getaddrinfo
        # 用户写 --bind-host '[::]' 时会报 gaierror(8)。这里把方括号剥掉再传进去。
        if bind_host is not None and server_address[0] == bind_host and ":" in bind_host:
            server_address = (_strip_brackets(bind_host), server_address[1])
        super().__init__(server_address, RequestHandlerClass)

    def server_bind(self):
        # 在 bind() 之前开 IPV6_V6ONLY=0，让 :: 通配同时接受 v4 mapped
        _enable_ipv6_dual_stack(self.socket)
        super().server_bind()


def run():
    """CLI 入口。"""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    # PyInstaller onefile 打包后 sys.path 上没有当前目录；
    # 显式 import 走 ucopy_server 包，避免 'No module named config'。
    from ucopy_server import config as server_config
    from ucopy_server import detach as server_detach
    args = server_config.parse_args()
    cfg = server_config.load_or_init(
        args.data_dir or server_config.DEFAULT_DATA_DIR
    )
    if args.port:
        cfg["port"] = args.port
    if args.bind_host:
        cfg["bind_host"] = args.bind_host
    # v1.7.0: --disable-https 写进 cfg（持久化）
    if args.disable_https:
        cfg["disable_https"] = True
    # CLI 显式传 --no-disable-https 不存在（action 没 store_false），但首次生成时默认 False
    cfg["data_dir"] = args.data_dir or cfg.get("data_dir") or server_config.DEFAULT_DATA_DIR
    init_app(cfg)

    if args.print_admin_password:
        print(f"admin 密码: {cfg['initial_admin']['password']}")
        return
    if args.init_only:
        return

    # ---- 后台模式：--stop / --detach 互斥 + 早处理 ----
    data_dir = cfg["data_dir"]
    log_file = args.log_file or os.path.join(data_dir, "server.log")
    pid_file = args.pid_file or os.path.join(data_dir, "server.pid")

    if args.stop:
        ok, msg = server_detach.stop(pid_file)
        print(f"{'OK' if ok else 'FAIL'}: {msg}")
        sys.exit(0 if ok else 1)

    if args.detach:
        # 先看是不是已经有 detached 进程在跑
        existing = server_detach.is_already_running(pid_file)
        if existing is not None:
            print(f"FAIL: PID {existing} 已在运行（{pid_file}）；先 --stop 再试")
            sys.exit(1)

        if sys.platform == "win32":
            # Windows: detach = 用 Popen 重启自己，父进程立刻 exit
            new_pid = server_detach.detach(log_file, pid_file)
            if new_pid > 0:
                print(f"[detached] PID={new_pid} 正在后台运行；日志: {log_file}")
                print(f"[detached] 停止: <exe> --data-dir {data_dir} --stop")
                sys.exit(0)
            else:
                print(f"[detached] 启动失败，详见 {log_file}", file=sys.stderr)
                sys.exit(1)
        else:
            # Unix: _daemonize_unix 会 fork；父进程会拿到子 PID
            new_pid = server_detach.detach(log_file, pid_file)
            if new_pid > 0:
                # 父进程路径：等 PID 文件出现（子进程异步写）
                for _ in range(100):
                    if os.path.exists(pid_file):
                        break
                    time.sleep(0.01)
                actual_pid = server_detach.read_pid(pid_file) or new_pid
                print(f"[detached] PID={actual_pid} 正在后台运行；日志: {log_file}")
                print(f"[detached] 停止: <exe> --data-dir {data_dir} --stop")
                print(f"[detached] 父进程退出，可关掉此终端")
                sys.exit(0)
            # new_pid == 0 说明是 daemon 进程本身，继续走服务启动
            # (后续会做 PID 文件清理 / 信号处理)

    # macOS launchd 默认启用（如未禁用）—— detach 模式下不冲突，但也不需要
    # v1.7.0: launchd 自启动必须 detach 模式（用户原话："服务程序无论是...当前启动为launchd自启动，
    # 都使用detach模式（无法指定为其他）"）
    if sys.platform == "darwin" and not args.no_autostart and not launchd.is_installed():
        LOG.info("[launchd] 首次启动，注册自启动到 ~/Library/LaunchAgents/")
        prog_args = [
            sys.executable,
            os.path.abspath(__file__),
            "--data-dir", data_dir,
            "--detach",  # ← 强制 detach，无法指定为其他
        ]
        # 把当前模式（端口 + disable_https）也带进去，让 launchd 启动时就用上正确配置
        prog_args.extend(["--port", str(int(cfg["port"]))])
        if cfg.get("bind_host"):
            prog_args.extend(["--bind-host", cfg["bind_host"]])
        if cfg.get("disable_https"):
            prog_args.append("--disable-https")
        ok = launchd.install(prog_args)
        LOG.info(f"[launchd] 注册{'成功' if ok else '失败'}")
    # v1.7.0: 若 launchd 已安装但 plist 里没有 --detach（v1.6.x 升级上来），
    # 重新 install 一下把 --detach 加进 plist。Install 是幂等的（先 uninstall 再 install）。
    elif sys.platform == "darwin" and not args.no_autostart and launchd.is_installed():
        try:
            plist_content = open(launchd.plist_path(), encoding="utf-8").read()
            if "--detach" not in plist_content:
                LOG.info("[launchd] 检测到旧版 plist 缺 --detach，重新注册")
                # 重新走一遍 install 流程
                prog_args = [
                    sys.executable,
                    os.path.abspath(__file__),
                    "--data-dir", data_dir,
                    "--detach",
                ]
                prog_args.extend(["--port", str(int(cfg["port"]))])
                if cfg.get("bind_host"):
                    prog_args.extend(["--bind-host", cfg["bind_host"]])
                if cfg.get("disable_https"):
                    prog_args.append("--disable-https")
                # 不卸载直接重写（保留 service 状态）
                import plistlib as _pl
                plist = _pl.load(open(launchd.plist_path(), "rb"))
                plist["ProgramArguments"] = prog_args
                with open(launchd.plist_path(), "wb") as f:
                    _pl.dump(plist, f)
                # 重启 launchd 服务让它重读 plist
                LOG.info(f"[launchd] plist 已更新（新增 --detach）")
        except Exception as e:
            LOG.warning(f"[launchd] 检查/更新 plist 失败: {e}")

    # 默认 IPv6 双栈（`::` + IPV6_V6ONLY=0），用户也可传 [::] / 具体 IPv6 / 0.0.0.0
    ThreadingHTTPSServer.address_family = _server_address_family(cfg["bind_host"])
    bind_host_for_socket = cfg["bind_host"]
    bind = (bind_host_for_socket, int(cfg["port"]))
    try:
        httpd = ThreadingHTTPSServer(bind, UCRequestHandler, bind_host=bind_host_for_socket)
    except OSError as e:
        LOG.error(f"绑定 {cfg['bind_host']}:{cfg['port']} 失败: {e}")
        LOG.error("提示：--bind-host 可设为：")
        LOG.error("  :: / [::]         # IPv4+IPv6 双栈（默认）")
        LOG.error("  0.0.0.0           # 仅 IPv4")
        LOG.error("  127.0.0.1         # 仅本机 IPv4")
        LOG.error("  具体 IPv6         # 如 2409:8a1e:...（不依赖 hosts）")
        sys.exit(1)
    httpd = make_server(cfg, httpd)

    # 启动日志显示：剥掉已剥过的方括号，避免 [[::]] 双重包裹
    disp_host = cfg["bind_host"]
    if ":" in disp_host and not (disp_host.startswith("[") and disp_host.endswith("]")):
        disp_host = f"[{disp_host}]"
    scheme = "http" if cfg.get("disable_https") else "https"
    LOG.info(f"UCopy Server 启动: {scheme}://{disp_host}:{cfg['port']}")
    LOG.info(f"数据目录: {cfg['data_dir']}")
    LOG.info(f"Web GUI:   {scheme}://localhost:{cfg['port']}/"
             f"（双栈 → 也能用 {scheme}://127.0.0.1:{cfg['port']}/）")

    # v1.7.3: db handle watchdog —— 每 30s 用一个独立 RO 连接测试一次写入可见性。
    # 事故复盘发现某些 long-running server 实例中 server 进程的 db handle 跟磁盘
    # 实际 db 文件脱钩（POST 200 返回，db 表里 0 行），重启后写入恢复。
    # 根因未确诊；此 watchdog 是"事故快速检测器"+ 事故时优雅 shutdown 让
    # launchd 自动重启 daemon。
    # 注意：实际注册在 _shutdown 定义之后，避免 forward reference 问题。
    _db_watchdog_state = {"stop_event": None, "extras": []}
    def _start_db_watchdog():
        db_path = os.path.join(cfg["data_dir"], "server.db")
        import threading
        stop_event = threading.Event()
        def loop():
            fail_streak = 0
            # 独立的 RW 连接做"测试写入"（不重用 DB_CONN，避免污染请求链路）
            try:
                test_conn = sqlite3.connect(
                    db_path, check_same_thread=False, isolation_level=None, timeout=5.0
                )
            except Exception as e:
                LOG.warning(f"[watchdog] 启动测试连接失败: {e}")
                return
            while not stop_event.is_set():
                stop_event.wait(30.0)
                if stop_event.is_set():
                    test_conn.close()
                    return
                marker = f"__ucopy_watchdog_{os.getpid()}_{int(time.time())}"
                try:
                    # 写 marker 到临时表（更干净，不污染 user_configs）：
                    test_conn.execute(
                        "CREATE TABLE IF NOT EXISTS __ucopy_watchdog ("
                        "  ts TEXT PRIMARY KEY"
                        ")"
                    )
                    test_conn.execute(
                        "INSERT INTO __ucopy_watchdog(ts) VALUES (?)", (marker,)
                    )
                    ro_conn = sqlite3.connect(
                        f"file:{db_path}?mode=ro", uri=True, timeout=2.0
                    )
                    rows = ro_conn.execute(
                        "SELECT ts FROM __ucopy_watchdog WHERE ts = ?", (marker,)
                    ).fetchall()
                    ro_conn.close()
                    test_conn.execute("DELETE FROM __ucopy_watchdog WHERE ts = ?", (marker,))
                    if not rows:
                        fail_streak += 1
                        LOG.warning(f"[watchdog] db handle diverged from disk "
                                    f"(streak={fail_streak}): RO 看不到刚才 RW 写的 marker={marker}")
                        if fail_streak >= 3:
                            LOG.error("[watchdog] 三次连续失败，触发优雅 shutdown 让 launchd 重启")
                            try:
                                os.kill(os.getpid(), signal.SIGTERM)
                            except Exception:
                                sys.exit(1)
                            return
                    else:
                        if fail_streak > 0:
                            LOG.info(f"[watchdog] db handle 已恢复 (was streak={fail_streak})")
                        fail_streak = 0
                except Exception as e:
                    LOG.warning(f"[watchdog] 例外: {type(e).__name__}: {e}")
                    fail_streak += 1
        t = threading.Thread(target=loop, name="ucopy-db-watchdog", daemon=True)
        t.start()
        def _stop_watchdog(*_):
            stop_event.set()
        _db_watchdog_state["stop_event"] = stop_event
        # 与 _shutdown 共存：先套一层，外层负责停 watchdog
        # 但 _shutdown 现在还没定义（line 1477 之后才定义）。这里用延迟绑定。
        _db_watchdog_state["extras"].append(stop_event)
        return _stop_watchdog
    stop_watchdog = _start_db_watchdog()

    def _shutdown(*_):
        # 先停 watchdog，让它把 thread 退出再 serve_forever shutdown
        if _db_watchdog_state.get("stop_event") is not None:
            _db_watchdog_state["stop_event"].set()
        LOG.info("收到退出信号，正在关闭...")
        ThreadingHTTPServer.shutdown(httpd)
        sys.exit(0)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    run()