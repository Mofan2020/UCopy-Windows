"""
ucopy/remote_client.py
======================

UCopy 客户端的"远程模式"客户端模块。

职责：
  - 与 ucopy-server (https) 建立 HTTPS 连接
  - 上传/下载配置文件（**剥除 security.password_hash / security.password_salt / security.qa**）
  - 列出服务器文件、下载文件、上传文件（带 sha256 比对）
  - 持久化信任的服务器证书指纹（避免每次自签证书弹窗）

依赖：仅标准库（urllib + ssl + json + hashlib + http.cookiejar）。
不引入 requests —— UCopy 主体打包体积不变。

Public API
----------
class RemoteClient
    .login(username, password) -> bool
    .logout() -> bool
    .fetch_config() -> dict         # 已脱敏的配置
    .push_config(cfg) -> bool       # 推送前会再剥一次 security.*（双层防御）
    .list_dir(rel_path) -> list     # 服务端列表 entries
    .download_file(rel_path, dest_path, progress=None) -> bool
    .upload_file(rel_path, src_path, sha256, size, mtime, progress=None) -> bool
    .check_sha256(rel_path, sha256) -> bool   # True 表示服务端内容相同，无需上传
    .delete_file(rel_path) -> bool

class TrustedServerStore
    .trust(server_url, fingerprint)
    .is_trusted(server_url) -> str|None     # 返回指纹或 None
    .get_fingerprint(server_url) -> str|None

函数：
    sanitize_config_for_remote(cfg) -> dict   # 剥除 security.* 的纯函数
    normalize_server_address(server_url) -> str  # 接受 host / ipv4:port / [ipv6]:port
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import io
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

PROTOCOL_VERSION = 1
TRUSTED_FILE = os.path.join(os.path.expanduser("~"), ".ucopy_trusted_servers.json")

# 剥除的字段（递归剥 security 整个子树）
REMOTE_REDACT_KEYS = ("password_hash", "password_salt", "qa")


def sanitize_config_for_remote(cfg: dict) -> dict:
    """
    把 cfg 复制一份，移除 security.password_hash / password_salt / qa。

    双层防御的一层：即使服务端也剥，客户端先剥一次，
    避免日志、错误响应、临时缓存泄漏密码哈希。

    递归只针对 dict（security 是 dict；qa 是 list 但 qa 是被整体移除，
    不会单独保留 list 元素）。
    """
    if not isinstance(cfg, dict):
        return cfg
    out = {}
    for k, v in cfg.items():
        if k == "security":
            sec = v if isinstance(v, dict) else {}
            sec_out = {
                sk: sv for sk, sv in sec.items()
                if sk not in REMOTE_REDACT_KEYS
            }
            out[k] = sec_out
        else:
            out[k] = sanitize_config_for_remote(v) if isinstance(v, dict) else v
    return out


def normalize_server_address(server_url: str) -> str:
    """
    接受：
        example.com        -> "https://example.com"
        example.com:8443   -> "https://example.com:8443"
        192.168.1.5:8443   -> "https://192.168.1.5:8443"
        2609:abcd::1       -> "https://[2609:abcd::1]"
        [2609:abcd::1]:8443-> "https://[2609:abcd::1]:8443"
        https://...        -> 原样返回（只校验格式）
    仅接受 https。拒绝 http/ftp/空/未指定 scheme。
    """
    s = (server_url or "").strip()
    if not s:
        raise ValueError("server_url is empty")
    if s.startswith("http://"):
        raise ValueError("远程模式仅支持 https（明文 http 会被拒绝）")
    if "://" in s and not s.startswith("https://"):
        raise ValueError(f"远程模式仅支持 https（{s.split('://', 1)[0]} 协议被拒绝）")
    # 不带 scheme：补 https://
    if "://" not in s:
        s = "https://" + s
    # 给裸 IPv6 字面量补方括号：识别 host 含 ":" 但不在 [...] 中。
    # IPv6 字面量含 ":" 且不是 "host:port" 的纯数字端口。
    # 判定：含 "::" 或 ":" 出现 ≥2 次 → IPv6；否则视为普通 host:port。
    after_scheme = s.split("://", 1)[1]
    if "/" in after_scheme:
        host_port = after_scheme.split("/", 1)[0]
    else:
        host_port = after_scheme
    if host_port and not host_port.startswith("["):
        if "::" in host_port or host_port.count(":") >= 2:
            # 纯 IPv6，无 port
            rebuilt = f"https://[{host_port}]"
            return rebuilt.rstrip("/")
        # 普通 host:port 或 host
        if ":" in host_port:
            host_only, sep, port = host_port.rpartition(":")
            if port.isdigit():
                rebuilt = f"https://{host_only}:{port}"
            else:
                rebuilt = f"https://{host_port}"
            return rebuilt.rstrip("/")
    return s.rstrip("/")


class TrustedServerStore:
    """持久化服务器指纹（SHA-256 of DER）。"""

    def __init__(self, path: str = TRUSTED_FILE):
        self.path = path
        self._cache = None

    def _load(self):
        if self._cache is not None:
            return self._cache
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            else:
                self._cache = {}
        except Exception:
            # 文件损坏 → 当成空信任列表（不要 raise，避免阻塞启动）
            self._cache = {}
        return self._cache

    def _save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            # 写失败 → 静默丢弃；下次再问用户
            pass

    @staticmethod
    def _key(server_url: str) -> str:
        return normalize_server_address(server_url).lower()

    def get_fingerprint(self, server_url: str) -> Optional[str]:
        return self._load().get(self._key(server_url))

    def is_trusted(self, server_url: str, current_fingerprint: str) -> bool:
        stored = self.get_fingerprint(server_url)
        return stored is not None and stored == current_fingerprint

    def trust(self, server_url: str, fingerprint: str) -> None:
        self._load()[self._key(server_url)] = fingerprint
        self._save()

    def forget(self, server_url: str) -> None:
        data = self._load()
        data.pop(self._key(server_url), None)
        self._save()


def _sha256_der(cert_der: bytes) -> str:
    return hashlib.sha256(cert_der).hexdigest()


def _make_ssl_context_trust_pinning(server_url: str, store: TrustedServerStore,
                                    trust_unknown_peer: bool = False) -> ssl.SSLContext:
    """
    返回一个 ssl.SSLContext，校验服务器证书 + 校验指纹。

    模式：
      - trust_unknown_peer=False（默认）：标准 CA 校验；自签会失败（让上层弹"信任"对话框）
      - trust_unknown_peer=True：跳过 CA 校验（只在用户已经确认了 fingerprint、想
        用纯 fingerprint pinning 模式时用；默认情况**不**走这条路径）

    Fingerpin 校验在 HTTPSHandler 里通过 *hook* 完成：
      - urllib 的 HTTPSHandler 在 build_opener 时被传入 ctx。
      - 我们包一个自定义 HTTPSHandler，在 do_open 拿到 socket 后、send request 前，
        调用 socket.getpeercert(binary_form=True) 拿 DER 算 fingerprint。

    为什么不只用 stats_callback：Python 3.7+ 的 stats_callback 在 LibreSSL（macOS
    自带）上不一定可用；pyright 静态分析也会误报。我们的方案直接 hook HTTPSHandler，
    不依赖平台特性。
    """
    ctx = ssl.create_default_context()
    if trust_unknown_peer:
        # CERT_NONE 与 check_hostname=True 互斥；要关掉 hostname 校验。
        # 安全靠 fingerprint pinning 保证，不依赖 CA。
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _pinning_https_handler_factory(server_url: str, store: TrustedServerStore,
                                   ctx: ssl.SSLContext,
                                   on_unknown_peer: Optional[Callable[[str], None]] = None
                                   ):
    """
    返回一个 urllib HTTPSHandler 子类：每次连服务器都会校验 peer cert 指纹。
    行为：
      - 没存 fingerprint → on_unknown_peer(fingerprint) 回调；ctx 用 CERT_NONE
        （因为回调才能拿到 SHA-256）
      - 存了 fingerprint → 比对；不一致抛 SSLCertVerificationError

    实现思路：
      AbstractHTTPHandler.do_open 接受 http_class (HTTPSConnection 子类)，
      我们传一个自定义类覆盖 connect() —— 在 connect() 里包装 socket 拿 DER。
      这样比 override do_open 更稳，不依赖父类私有变量。
    """
    parent_handler = urllib.request.HTTPSHandler(context=ctx)

    def pin_after_connect(conn):
        sock = getattr(conn, "sock", None)
        if sock is None:
            return
        try:
            der = sock.getpeercert(binary_form=True)
        except Exception:
            return
        if not der:
            return
        cur_fp = _sha256_der(der)
        stored_fp = store.get_fingerprint(server_url)
        if stored_fp is None:
            if on_unknown_peer:
                try:
                    on_unknown_peer(cur_fp)
                except Exception:
                    pass
            return
        if cur_fp != stored_fp:
            raise ssl.SSLCertVerificationError(
                f"服务器证书指纹与已信任的不一致 "
                f"（cur={cur_fp[:12]}… stored={stored_fp[:12]}…）"
            )

    # 利用 HTTPSHandler.do_open 的最后一步（返回 conn 前）校验。
    # do_open 内部创建连接 + 发送 + 接收，最后 return conn。
    # 我们把 do_open 包一层：super().do_open -> conn = ... -> pin(conn)
    # 但 HTTPSHandler 没有暴露 http_class 的 override hook，所以直接 monkey-patch do_open。
    original_do_open = parent_handler.do_open

    def patched_do_open(http_class, req, **http_conn_args):
        # 临时把 http_class.connect 包一层，插入 fingerprint 校验
        original_connect = http_class.connect

        def wrapped_connect(self_):
            original_connect(self_)
            pin_after_connect(self_)

        http_class.connect = wrapped_connect
        try:
            return original_do_open(http_class, req, **http_conn_args)
        finally:
            http_class.connect = original_connect

    parent_handler.do_open = patched_do_open
    return parent_handler


class RemoteError(Exception):
    def __init__(self, message: str, code: str = "internal", http_status: int = 0):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


class RemoteClient:
    """
    UCopy 远程模式客户端。

    用法：
        store = TrustedServerStore()
        client = RemoteClient(server_url, store=store)
        # 第一次连会抛 RemoteErrorSslUnknownPeer → 弹窗让用户确认 →
        #   store.trust(url, fingerprint) → 重新 new client
        client.login(username, password)
        cfg = client.fetch_config()
        ...
    """

    def __init__(self, server_url: str, store: Optional[TrustedServerStore] = None):
        self.server_url = normalize_server_address(server_url)
        self.store = store or TrustedServerStore()
        self._jar = http.cookiejar.CookieJar()
        self._opener = None  # lazy init
        # v1.6.0: API key 认证状态
        self._api_key: Optional[str] = None
        self._client_id: Optional[str] = None
        self._key_id: Optional[int] = None
        self._heartbeat_interval: int = 40

    # ---------- low-level request ----------
    def _build_opener(self):
        # 流程：
        #   - 如果 store 里已存 fingerprint → 用 CERT_NONE + 后续 pin 校验
        #     （自签 CA 不认，所以要绕开 CA 校验）
        #   - 如果没存 → 用 CERT_REQUIRED（标准 CA 校验），自签会抛
        #     SSLCertVerificationError，UI 层捕获后让用户"信任"，调 store.trust，
        #     然后**重新 new client**（下次进"已信任"分支）
        if self.store.get_fingerprint(self.server_url):
            trust_unknown_peer = True
        else:
            trust_unknown_peer = False
        ctx = _make_ssl_context_trust_pinning(self.server_url, self.store,
                                              trust_unknown_peer=trust_unknown_peer)
        pinning_handler = _pinning_https_handler_factory(
            self.server_url, self.store, ctx,
            on_unknown_peer=None,
        )
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            pinning_handler,
        )

    def _request(self, method: str, path: str, *, body=None,
                 content_type=None, parse_json=True):
        url = self.server_url + path
        headers = {"X-UCopy-Protocol": str(PROTOCOL_VERSION)}
        data = None
        if body is not None:
            if content_type is None and not isinstance(body, (bytes, bytearray)):
                content_type = "application/json"
                body = json.dumps(body).encode("utf-8")
            if isinstance(body, str):
                body = body.encode("utf-8")
            data = body
            headers["Content-Length"] = str(len(data))
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        if self._opener is None:
            self._opener = self._build_opener()
        try:
            with self._opener.open(req, timeout=30) as resp:
                payload = resp.read()
                if parse_json:
                    try:
                        return json.loads(payload.decode("utf-8")), resp.status
                    except Exception as e:
                        raise RemoteError(f"服务器返回非 JSON: {e}", "internal", resp.status)
                return payload, resp.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            code = (obj or {}).get("code", "http_error")
            msg = (obj or {}).get("error", raw) or f"HTTP {e.code}"
            raise RemoteError(msg, code, e.code)
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise RemoteError(str(reason), "ssl_cert", 0)
            if isinstance(reason, ssl.SSLError):
                raise RemoteError(str(reason), "ssl_error", 0)
            raise RemoteError(str(reason), "network", 0)
        except (socket.timeout, TimeoutError):
            raise RemoteError("连接超时", "timeout", 0)
        except OSError as e:
            raise RemoteError(str(e), "network", 0)

    # ---------- public API ----------
    def login(self, username: str, password: str) -> dict:
        obj, _ = self._request("POST", "/api/v1/login",
                               body={"username": username, "password": password})
        if not obj.get("ok"):
            raise RemoteError("登录失败", "invalid_credentials", 401)
        return obj

    def login_with_key(self, api_key: str, client_id: str) -> dict:
        """v1.6.0: 用 API key 登录（替代 username/password）。

        api_key 格式：<token>.<secret>
        返回 {ok, username, role, quota_mb, key_id, client_id,
              previous_client_id, heartbeat_interval_seconds,
              lock_timeout_seconds}
        如果 previous_client_id 非空 = 抢占式踢掉了旧客户端。
        """
        obj, _ = self._request("POST", "/api/v1/auth/key",
                               body={"key": api_key, "client_id": client_id})
        if not obj.get("ok"):
            raise RemoteError(obj.get("error") or "登录失败",
                              obj.get("code") or "invalid_key", 401)
        # 记住 key/client_id 供 heartbeat 用
        self._api_key = api_key
        self._client_id = client_id
        self._key_id = obj.get("key_id")
        self._heartbeat_interval = int(obj.get("heartbeat_interval_seconds") or 40)
        return obj

    def heartbeat(self) -> dict:
        """v1.6.0: 心跳保持独占锁。

        返回 {ok, heartbeat_interval_seconds}
        锁被其他客户端接管 → RemoteError(code='key_lock_lost', http_status=410)
        """
        if not getattr(self, "_api_key", None) or not getattr(self, "_client_id", None):
            raise RemoteError("未用 key 登录，无法发心跳", "not_key_authenticated", 0)
        obj, _ = self._request("POST", "/api/v1/auth/heartbeat",
                               body={"key": self._api_key,
                                     "client_id": self._client_id})
        if not obj.get("ok"):
            raise RemoteError(obj.get("error") or "心跳失败",
                              obj.get("code") or "heartbeat_failed", 0)
        self._heartbeat_interval = int(obj.get("heartbeat_interval_seconds") or 40)
        return obj

    @property
    def key_id(self):
        return getattr(self, "_key_id", None)

    @property
    def client_id(self):
        return getattr(self, "_client_id", None)

    @property
    def heartbeat_interval(self) -> int:
        """服务端建议的心跳间隔（秒）。默认 40。"""
        return int(getattr(self, "_heartbeat_interval", 40) or 40)

    def logout(self) -> None:
        try:
            self._request("POST", "/api/v1/logout")
        except RemoteError:
            pass

    def me(self) -> dict:
        obj, _ = self._request("GET", "/api/v1/me")
        return obj

    def fetch_config(self) -> dict:
        obj, _ = self._request("GET", "/api/v1/config")
        if not obj.get("ok"):
            raise RemoteError("拉取配置失败", "internal", 0)
        # 服务端已脱敏；客户端再脱一次（双层防御）
        cfg = obj.get("config", {})
        return sanitize_config_for_remote(cfg)

    def push_config(self, cfg: dict) -> dict:
        sanitized = sanitize_config_for_remote(cfg)
        obj, _ = self._request("PUT", "/api/v1/config",
                               body={"config": sanitized})
        return obj

    def list_dir(self, rel_path: str = "") -> dict:
        qs = urllib.parse.urlencode({"path": rel_path})
        obj, _ = self._request("GET", f"/api/v1/files?{qs}")
        return obj

    def download_file(self, rel_path: str, dest_path: str,
                      progress: Optional[Callable[[int, int], None]] = None) -> bool:
        qs = urllib.parse.urlencode({"path": rel_path})
        url = self.server_url + f"/api/v1/files/download?{qs}"
        req = urllib.request.Request(url, headers={"X-UCopy-Protocol": str(PROTOCOL_VERSION)})
        if self._opener is None:
            self._opener = self._build_opener()
        try:
            with self._opener.open(req, timeout=300) as resp:
                total = resp.headers.get("Content-Length")
                total_i = int(total) if (total and total.isdigit()) else -1
                read = 0
                # 写到 .part 再 rename，避免中途崩溃留半截
                tmp = dest_path + ".part"
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        read += len(chunk)
                        if progress:
                            try:
                                progress(read, total_i)
                            except Exception:
                                pass
                os.replace(tmp, dest_path)
                return True
        except RemoteError:
            raise
        except Exception as e:
            try:
                if os.path.exists(dest_path + ".part"):
                    os.remove(dest_path + ".part")
            except Exception:
                pass
            raise RemoteError(str(e), "network", 0)

    def upload_file(self, rel_path: str, src_path: str,
                    sha256: str, size: int, mtime: float,
                    progress: Optional[Callable[[int, int], None]] = None) -> dict:
        """
        multipart/form-data 上传，附带 sha256/size/mtime 字段。

        实现：把整个 multipart body 一次性构造成 bytes（接受内存开销），
        然后用 http.client.HTTPSConnection 直连。流式实现 + keep-alive 池
        在 BaseHTTPRequestHandler (Connection: close) 模式下挂住，实测 30s+。
        一次性构造是更稳的方案 —— 我们的上传低频（一次复制几十个文件），
        单文件 body 通常 <100MB，单文件全内存完全可接受。
        """
        from http.client import HTTPSConnection

        boundary = "----UCopyBoundary" + os.urandom(8).hex()
        qs = urllib.parse.urlencode({"path": rel_path})
        url_parts = urllib.parse.urlsplit(self.server_url)
        host = url_parts.hostname
        port = url_parts.port or 443

        def field(name: str, value: str) -> bytes:
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")

        file_header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(src_path)}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        closing = f"\r\n--{boundary}--\r\n".encode("utf-8")

        # 一次性构造 body（progress 回调：构造时一次性 0% → 100%）
        with open(src_path, "rb") as f:
            file_bytes = f.read()
        body = (
            field("sha256", sha256) +
            field("size", str(size)) +
            field("mtime", str(mtime)) +
            file_header +
            file_bytes +
            closing
        )
        total_size = len(body)
        if progress:
            try:
                progress(total_size, total_size)
            except Exception:
                pass

        # Cookie
        cookie_header = ""
        for c in self._jar:
            cookie_header += f"{c.name}={c.value}; "
        cookie_header = cookie_header.rstrip("; ")

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(total_size),
            "X-UCopy-Protocol": str(PROTOCOL_VERSION),
            "Connection": "close",
            "Host": f"{host}:{port}",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header

        # SSL context
        if self.store.get_fingerprint(self.server_url):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED

        url_path = f"/api/v1/files/upload?{qs}"
        try:
            raw_conn = HTTPSConnection(host, port, context=ctx, timeout=600)
            try:
                raw_conn.request("POST", url_path, body=body, headers=headers)
                resp = raw_conn.getresponse()
                payload = resp.read(64 * 1024 + 1)
                self._pin_after_connect(raw_conn)
                if 200 <= resp.status < 300:
                    try:
                        return json.loads(payload.decode("utf-8"))
                    except Exception:
                        return {"ok": False, "error": "服务器返回非 JSON"}
                else:
                    try:
                        obj = json.loads(payload.decode("utf-8"))
                    except Exception:
                        obj = None
                    code = (obj or {}).get("code", "http_error")
                    msg = (obj or {}).get("error", payload.decode("utf-8", errors="replace"))
                    raise RemoteError(msg or f"HTTP {resp.status}", code, resp.status)
            finally:
                try:
                    raw_conn.close()
                except Exception:
                    pass
        except ssl.SSLCertVerificationError as e:
            raise RemoteError(str(e), "ssl_cert", 0)
        except ssl.SSLError as e:
            raise RemoteError(str(e), "ssl_error", 0)
        except (socket.timeout, TimeoutError):
            raise RemoteError("上传超时", "timeout", 0)
        except OSError as e:
            raise RemoteError(str(e), "network", 0)

    def _pin_after_connect(self, conn):
        """对直连的 conn 做 fingerprint pin 校验（upload_file 用）。"""
        sock = getattr(conn, "sock", None)
        if sock is None:
            return
        try:
            der = sock.getpeercert(binary_form=True)
        except Exception:
            return
        if not der:
            return
        cur_fp = _sha256_der(der)
        stored_fp = self.store.get_fingerprint(self.server_url)
        if stored_fp is None:
            return  # 未走 trust 流程
        if cur_fp != stored_fp:
            raise ssl.SSLCertVerificationError(
                f"服务器证书指纹与已信任的不一致 "
                f"（cur={cur_fp[:12]}… stored={stored_fp[:12]}…）"
            )

    def check_sha256(self, rel_path: str, sha256: str) -> bool:
        qs = urllib.parse.urlencode({"path": rel_path, "sha256": sha256})
        try:
            obj, _ = self._request("GET", f"/api/v1/files/check?{qs}")
            return bool(obj.get("match"))
        except RemoteError as e:
            # 404 = 不存在/不同 → 返回 False
            if e.http_status == 404:
                return False
            raise

    def delete_file(self, rel_path: str) -> bool:
        qs = urllib.parse.urlencode({"path": rel_path})
        obj, _ = self._request("DELETE", f"/api/v1/files?{qs}")
        return bool(obj.get("ok"))

    # ---------- cert pinning helpers ----------
    def get_current_fingerprint(self) -> Optional[str]:
        """连一次服务器拿当前证书指纹（用新 ctx，不做 pin 校验）。"""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = self.server_url + "/api/v1/ping"
        req = urllib.request.Request(url, headers={"X-UCopy-Protocol": str(PROTOCOL_VERSION)})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                # peer cert from socket
                der = None
                try:
                    der = resp.fp.raw._sock.getpeercert(binary_form=True)
                except Exception:
                    pass
                if der:
                    return _sha256_der(der)
        except Exception:
            pass
        return None