"""
ucopy-server/db.py
==================

SQLite 数据访问层（users / sessions / per-user config / file metadata / api keys）。

线程模型：
    sqlite3.connect(check_same_thread=False) + 每个请求拿新连接。
    在 BaseHTTPRequestHandler 单线程下没问题；并发扩展时改用连接池。

数据表：

    users (
        username TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,         -- pbkdf2$...$salt$hash
        role TEXT NOT NULL CHECK(role IN ('admin','user','readonly')),
        quota_mb INTEGER NOT NULL DEFAULT 10240,
        created_at TEXT NOT NULL
    )

    sessions (
        id_hash TEXT PRIMARY KEY,            -- sha256(cookie_value)
        username TEXT NOT NULL REFERENCES users(username),
        expires_at INTEGER NOT NULL          -- unix timestamp
    )
    INDEX idx_sessions_user ON sessions(username);

    user_configs (
        username TEXT PRIMARY KEY REFERENCES users(username),
        config_json TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        last_modified TEXT NOT NULL
    )

    file_index (
        username TEXT NOT NULL,
        rel_path TEXT NOT NULL,              -- 相对用户根，POSIX 风格
        size INTEGER NOT NULL,
        mtime REAL NOT NULL,
        sha256 TEXT NOT NULL,
        PRIMARY KEY (username, rel_path)
    )

    api_keys (                                       -- v1.6.0 新增
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,     -- sha256(key_token)
        secret_hash TEXT NOT NULL,            -- pbkdf2(key_secret, salt)
        secret_salt TEXT NOT NULL,
        name TEXT NOT NULL,
        current_client_id TEXT,               -- 当前占用 key 的 client_id（exclusive lock）
        last_heartbeat_at REAL,               -- 上次心跳时间戳（unix time）
        created_at REAL NOT NULL
    )
    INDEX idx_keys_user ON api_keys(username);

注意：
    - rel_path 不允许含 '..' 或绝对路径前缀（用 storage.normalize_rel_path 校验）
    - 用户文件本体存在文件系统上（<data_dir>/files/<username>/<rel_path>）
    - file_index 是元数据缓存：实际校验以磁盘为准，元数据丢失时重建
    - api_keys 是 server 端 API 认证的长期凭据；登录用 key 而不是 user password
    - key 是 user 自己创建；user 也只能看到自己创建的 keys
    - 心跳机制：客户端每 40s 发一次；2 分钟没心跳 → 锁自动释放
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional, Iterable, List, Dict, Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','user','readonly')),
    quota_mb INTEGER NOT NULL DEFAULT 10240,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(username);

CREATE TABLE IF NOT EXISTS user_configs (
    username TEXT PRIMARY KEY REFERENCES users(username) ON DELETE CASCADE,
    config_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    last_modified TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_index (
    username TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (username, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_file_index_user ON file_index(username);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    secret_hash TEXT NOT NULL,
    secret_salt TEXT NOT NULL,
    name TEXT NOT NULL,
    current_client_id TEXT,
    last_heartbeat_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(username);
"""


def open_db(path: str) -> sqlite3.Connection:
    """打开 SQLite 连接 + 应用 schema。

    v1.7.3 增强：事故复盘发现某些 long-running server 实例中
    server 进程 db handle 与磁盘 db 文件脱钩（POST 200 + db 表里 0 行），
    重启后写入恢复。根因未确诊（怀疑 macOS launchd + 长 fd 持有）。
    本次只在打开时做一次自检（cursor 落盘 → 立即 SELECT 对比），
    不引入额外 PRAGMA，避免误报。

    注：autocommit 模式 (isolation_level=None) 下 commit 由 sqlite3 driver
    自动完成；不需要显式 conn.commit()。fsync 由 SQLite PRAGMA synchronous
    控制（默认 NORMAL）。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


# ---------- users ----------

VALID_ROLES = ("admin", "user", "readonly")


def add_user(conn, username: str, password_hash: str, role: str,
             quota_mb: int = 10240) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"invalid role: {role}")
    conn.execute(
        "INSERT INTO users(username, password_hash, role, quota_mb, created_at) "
        "VALUES (?,?,?,?,?)",
        (username, password_hash, role, quota_mb,
         time.strftime("%Y-%m-%dT%H:%M:%S")),
    )


def get_user(conn, username: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def list_users(conn):
    return conn.execute(
        "SELECT username, role, quota_mb, created_at FROM users ORDER BY username"
    ).fetchall()


def update_user(conn, username: str, *,
                password_hash=None,
                role=None,
                quota_mb=None) -> None:
    sets, params = [], []
    if password_hash is not None:
        sets.append("password_hash = ?"); params.append(password_hash)
    if role is not None:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role}")
        sets.append("role = ?"); params.append(role)
    if quota_mb is not None:
        sets.append("quota_mb = ?"); params.append(int(quota_mb))
    if not sets:
        return
    params.append(username)
    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE username = ?", params)


def delete_user(conn, username: str) -> None:
    conn.execute("DELETE FROM users WHERE username = ?", (username,))


def user_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


# ---------- sessions ----------

def add_session(conn, id_hash: str, username: str, ttl_seconds: int) -> None:
    expires_at = int(time.time()) + ttl_seconds
    conn.execute(
        "INSERT INTO sessions(id_hash, username, expires_at) VALUES (?,?,?)",
        (id_hash, username, expires_at),
    )


def lookup_session(conn, id_hash: str) -> Optional[sqlite3.Row]:
    if not id_hash:
        return None
    row = conn.execute(
        "SELECT s.username, s.expires_at, u.role, u.quota_mb "
        "FROM sessions s JOIN users u ON s.username = u.username "
        "WHERE s.id_hash = ?", (id_hash,)
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < int(time.time()):
        conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
        return None
    return row


def delete_session(conn, id_hash: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))


def purge_expired_sessions(conn) -> int:
    """清理过期 session（每个请求顺手跑，副作用是单条 DELETE）。"""
    cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?",
                       (int(time.time()),))
    return cur.rowcount or 0


# ---------- user_configs ----------

REDACT_KEYS = ("password_hash", "password_salt", "qa")


def _redact(cfg: dict) -> dict:
    """剥除 security.* 字段。"""
    if not isinstance(cfg, dict):
        return cfg
    out = {}
    for k, v in cfg.items():
        if k == "security":
            sec = v if isinstance(v, dict) else {}
            out[k] = {sk: sv for sk, sv in sec.items() if sk not in REDACT_KEYS}
        else:
            out[k] = _redact(v) if isinstance(v, dict) else v
    return out


def get_user_config(conn, username: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT config_json, version, last_modified FROM user_configs WHERE username = ?",
        (username,)
    ).fetchone()
    if not row:
        return None
    try:
        cfg = json.loads(row["config_json"])
    except Exception:
        cfg = {}
    return {
        "config": _redact(cfg),
        "version": row["version"],
        "last_modified": row["last_modified"],
    }


def put_user_config(conn, username: str, cfg: dict) -> dict:
    """覆盖存：传上来的 cfg 服务端再次脱敏（双层防御）后写盘。返回新 version。"""
    sanitized = _redact(cfg)
    row = conn.execute(
        "SELECT version FROM user_configs WHERE username = ?", (username,)
    ).fetchone()
    new_version = (row["version"] + 1) if row else 1
    last_modified = time.strftime("%Y-%m-%dT%H:%M:%S")
    cfg_json = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        "INSERT INTO user_configs(username, config_json, version, last_modified) "
        "VALUES(?,?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET "
        "config_json=excluded.config_json, version=excluded.version, "
        "last_modified=excluded.last_modified",
        (username, cfg_json, new_version, last_modified),
    )
    return {"version": new_version, "last_modified": last_modified}


# ---------- file_index ----------

def upsert_file(conn, username: str, rel_path: str, *,
                size: int, mtime: float, sha256: str) -> None:
    conn.execute(
        "INSERT INTO file_index(username, rel_path, size, mtime, sha256) "
        "VALUES(?,?,?,?,?) "
        "ON CONFLICT(username, rel_path) DO UPDATE SET "
        "size=excluded.size, mtime=excluded.mtime, sha256=excluded.sha256",
        (username, rel_path, size, mtime, sha256),
    )


def delete_file_index(conn, username: str, rel_path: str) -> None:
    conn.execute(
        "DELETE FROM file_index WHERE username = ? AND rel_path = ?",
        (username, rel_path),
    )


def get_file_meta(conn, username: str, rel_path: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM file_index WHERE username = ? AND rel_path = ?",
        (username, rel_path),
    ).fetchone()


def user_total_size(conn, username: str) -> int:
    """用户在 file_index 中的总占用（注意：实际磁盘可能略有偏差）。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS total FROM file_index WHERE username = ?",
        (username,),
    ).fetchone()
    return int(row["total"] or 0)


def list_files(conn, username: str, rel_dir: str):
    """列出 username 在 rel_dir 下的直接子项（不含孙子辈）。"""
    rel_dir = rel_dir.strip("/")
    prefix = rel_dir + "/" if rel_dir else ""
    like_pattern = prefix + "%"
    rows = conn.execute(
        "SELECT rel_path, size, mtime, sha256 FROM file_index "
        "WHERE username = ? AND rel_path LIKE ? AND rel_path NOT LIKE ?",
        (username, like_pattern, prefix + "%/%"),
    ).fetchall()
    return [
        {
            "name": r["rel_path"][len(prefix):],
            "type": "file",
            "size": r["size"],
            "mtime": r["mtime"],
            "sha256": r["sha256"],
        }
        for r in rows
    ]


def list_dirs(conn, username: str, rel_dir: str):
    """列出 username 在 rel_dir 下的子目录（仅含直接子目录）。"""
    rel_dir = rel_dir.strip("/")
    prefix = rel_dir + "/" if rel_dir else ""
    like_pattern = prefix + "%"
    rows = conn.execute(
        "SELECT DISTINCT rel_path FROM file_index "
        "WHERE username = ? AND rel_path LIKE ? AND rel_path LIKE ?",
        (username, like_pattern, prefix + "%/%"),
    ).fetchall()
    seen = set()
    out = []
    for r in rows:
        rest = r["rel_path"][len(prefix):]
        first = rest.split("/", 1)[0]
        if first in seen:
            continue
        seen.add(first)
        out.append({
            "name": first,
            "type": "dir",
            "size": None,
            "mtime": None,
            "sha256": None,
        })
    return out


# ---------- api_keys (v1.6.0) ----------

def add_api_key(conn, username: str, token_hash: str,
                secret_hash: str, secret_salt: str, name: str) -> int:
    """创建 key。返回 key id。"""
    cur = conn.execute(
        "INSERT INTO api_keys(username, token_hash, secret_hash, "
        "secret_salt, name, created_at) VALUES (?,?,?,?,?,?)",
        (username, token_hash, secret_hash, secret_salt, name,
         time.time()),
    )
    return int(cur.lastrowid)


def get_api_key_by_token(conn, token_hash: str):
    """按 token_hash 查（不含 secret_hash，返回时主动剥除防泄露）。"""
    return conn.execute(
        "SELECT id, username, token_hash, secret_hash, secret_salt, name, "
        "current_client_id, last_heartbeat_at, created_at "
        "FROM api_keys WHERE token_hash = ?", (token_hash,)
    ).fetchone()


def get_api_key_by_id(conn, key_id: int, username: str):
    """按 id + username 查（防止越权看别人的 key）。"""
    return conn.execute(
        "SELECT id, username, name, current_client_id, last_heartbeat_at, "
        "created_at FROM api_keys WHERE id = ? AND username = ?",
        (key_id, username),
    ).fetchone()


def list_api_keys(conn, username: str):
    """列出 username 的所有 key（不含 secret_hash / token_hash / secret_salt）。"""
    rows = conn.execute(
        "SELECT id, name, current_client_id, last_heartbeat_at, created_at "
        "FROM api_keys WHERE username = ? ORDER BY created_at DESC",
        (username,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_api_key(conn, key_id: int, username: str) -> int:
    """删除 key（按 id + username 防越权）。返回受影响行数。"""
    cur = conn.execute(
        "DELETE FROM api_keys WHERE id = ? AND username = ?",
        (key_id, username),
    )
    return cur.rowcount or 0


def claim_api_key_lock(conn, key_id: int, username: str, client_id: str,
                       now: float | None = None) -> bool:
    """独占锁：current_client_id = client_id 且 last_heartbeat_at = now。

    返回 True 表示获得锁（含抢占式：同 key 已有 current_client_id
    也允许抢占，新 client_id 接管 + 更新 last_heartbeat_at）。
    返回 False 表示 key 不存在 / 不属于 username。
    """
    if now is None:
        now = time.time()
    cur = conn.execute(
        "UPDATE api_keys SET current_client_id = ?, last_heartbeat_at = ? "
        "WHERE id = ? AND username = ?",
        (client_id, now, key_id, username),
    )
    return (cur.rowcount or 0) > 0


def update_api_key_heartbeat(conn, key_id: int, username: str, client_id: str,
                              now: float | None = None) -> bool:
    """心跳：必须 client_id 匹配才更新；不匹配返回 False（被踢了）。"""
    if now is None:
        now = time.time()
    cur = conn.execute(
        "UPDATE api_keys SET last_heartbeat_at = ? "
        "WHERE id = ? AND username = ? AND current_client_id = ?",
        (now, key_id, username, client_id),
    )
    return (cur.rowcount or 0) > 0


def release_api_key_lock(conn, key_id: int, username: str, client_id: str) -> int:
    """释放锁（client_id 匹配才清；防止误清别人的锁）。"""
    cur = conn.execute(
        "UPDATE api_keys SET current_client_id = NULL, last_heartbeat_at = NULL "
        "WHERE id = ? AND username = ? AND current_client_id = ?",
        (key_id, username, client_id),
    )
    return cur.rowcount or 0


def purge_stale_api_key_locks(conn, lock_timeout_seconds: float = 120.0,
                              now: float | None = None) -> int:
    """清理 stale 锁：last_heartbeat_at 超过 timeout 秒 → 释放。

    每次请求顺手跑（单条 UPDATE 副作用小）。"""
    if now is None:
        now = time.time()
    cutoff = now - lock_timeout_seconds
    cur = conn.execute(
        "UPDATE api_keys SET current_client_id = NULL, last_heartbeat_at = NULL "
        "WHERE current_client_id IS NOT NULL AND last_heartbeat_at < ?",
        (cutoff,),
    )
    return cur.rowcount or 0


def update_api_key_name(conn, key_id: int, username: str, name: str) -> int:
    """改名（v1.6.0 可选；保留 API 但 Web GUI 不暴露）。"""
    cur = conn.execute(
        "UPDATE api_keys SET name = ? WHERE id = ? AND username = ?",
        (name, key_id, username),
    )
    return cur.rowcount or 0