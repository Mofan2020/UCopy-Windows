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
import socket
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


def init_app(cfg: dict):
    """由 main() 调用：初始化数据目录 / DB / 证书 / admin 账号。"""
    global CFG, DATA_DIR, DB_CONN, SESSIONS_TTL, COOKIE_NAME
    CFG = cfg
    DATA_DIR = cfg["data_dir"]
    SESSIONS_TTL = int(cfg.get("session_ttl_seconds", 86400))
    COOKIE_NAME = cfg.get("cookie_name", "ucopy_session")

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
        cookie = (
            f"{COOKIE_NAME}={token}; HttpOnly; Secure; SameSite=Strict; "
            f"Path=/; Max-Age={SESSIONS_TTL}"
        )
        self.send_header("Set-Cookie", cookie)

    def _clear_session_cookie(self):
        cookie = f"{COOKIE_NAME}=deleted; HttpOnly; Secure; Path=/; Max-Age=0"
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

            if path == "/api/v1/launchd" and method == "GET":
                return self._api_launchd_status()
            if path == "/api/v1/launchd/install" and method == "POST":
                return self._api_launchd_install()
            if path == "/api/v1/launchd/uninstall" and method == "POST":
                return self._api_launchd_uninstall()

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
        exec_cmd = f"{sys.executable} {os.path.abspath(__file__)}"
        ok = launchd.install(exec_cmd)
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
        static_dir = os.path.join(THIS_DIR, "static")
        if not path:
            return self._serve_index()
        full = os.path.join(static_dir, path)
        if os.path.isfile(full):
            mime, _ = mimetypes.guess_type(full)
            mime = mime or "application/octet-stream"
            with open(full, "rb") as f:
                data = f.read()
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
        # index.html 在 static/ 下
        index_path = os.path.join(THIS_DIR, "static", "index.html")
        if not os.path.exists(index_path):
            return self._err(404, "not_found", "Web GUI 未安装（static/index.html 不存在）")
        with open(index_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)


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
    """wrap httpd with TLS（基于已有 ctx）。"""
    cert_path, key_path = cert.ensure_cert(cfg["data_dir"])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


class ThreadingHTTPSServer(ThreadingHTTPServer):
    """Python 3.7+ 的 ThreadingHTTPServer 已经线程化了，继承即可用。"""
    daemon_threads = True
    allow_reuse_address = True


def run():
    """CLI 入口。"""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    import config as server_config
    args = server_config.parse_args()
    cfg = server_config.load_or_init(
        args.data_dir or server_config.DEFAULT_DATA_DIR
    )
    if args.port:
        cfg["port"] = args.port
    if args.bind_host:
        cfg["bind_host"] = args.bind_host
    cfg["data_dir"] = args.data_dir or cfg.get("data_dir") or server_config.DEFAULT_DATA_DIR
    init_app(cfg)

    if args.print_admin_password:
        print(f"admin 密码: {cfg['initial_admin']['password']}")
        return
    if args.init_only:
        return

    # macOS launchd 默认启用（如未禁用）
    if sys.platform == "darwin" and not args.no_autostart and not launchd.is_installed():
        LOG.info("[launchd] 首次启动，注册自启动到 ~/Library/LaunchAgents/")
        ok = launchd.install(f"{sys.executable} {os.path.abspath(__file__)}")
        LOG.info(f"[launchd] 注册{'成功' if ok else '失败'}")

    bind = (cfg["bind_host"], int(cfg["port"]))
    try:
        httpd = ThreadingHTTPServer(bind, UCRequestHandler)
    except OSError as e:
        LOG.error(f"绑定 {cfg['bind_host']}:{cfg['port']} 失败: {e}")
        LOG.error("提示：bind-host 可设 '0.0.0.0' / '127.0.0.1' / 具体 IP，"
                  "或用 '[::]'（注意 IPv6 通配符在 macOS 上要进 /etc/hosts "
                  "里加 ::1 localhost）")
        sys.exit(1)
    httpd = make_server(cfg, httpd)

    LOG.info(f"UCopy Server 启动: https://[{cfg['bind_host']}]:{cfg['port']}")
    LOG.info(f"数据目录: {cfg['data_dir']}")
    LOG.info(f"Web GUI:   https://localhost:{cfg['port']}/")

    def _shutdown(*_):
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