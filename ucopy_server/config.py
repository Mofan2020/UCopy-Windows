"""
ucopy-server/config.py
======================

服务器配置：监听地址 / 端口 / 数据目录 / 初始管理员账号。

约定：
    - 配置文件存放在 <data_dir>/server.json
    - 第一次启动没有这个文件时，按下面硬编码默认生成 + 写盘
    - 用户通过 Web GUI 或手编 server.json 修改（不会写到一半列配置覆盖）

也负责 CLI 参数解析：--port / --data-dir / --no-autostart 等。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys

DEFAULT_PORT = 8443
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), "UCopyServer")
# 默认 IPv4 通配：避免 macOS 上 '::' 通配需要 /etc/hosts 配 ::1 localhost 才能解析的问题。
# 想用 IPv6 监听：传 --bind-host '[::]'（注意中括号必须），或具体地址如 2409:8a1e:...。
DEFAULT_BIND_HOST = "0.0.0.0"

DEFAULT_CONFIG = {
    "port": DEFAULT_PORT,
    "bind_host": DEFAULT_BIND_HOST,
    "session_ttl_seconds": 86400,        # 24h
    "cookie_name": "ucopy_session",
    "initial_admin": {
        "username": "admin",
        # 首次启动会生成随机密码写到 server.json + 终端打印
        "password": None,
    },
    # macOS 自启动：launchd plist 文件名（com.skyc8266.ucopy-server 前缀约定）
    "launchd_label": "com.skyc8266.ucopy-server",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="ucopy-server",
        description="UCopy 远程模式配套服务器（HTTPS + 多用户 + Web GUI）",
    )
    parser.add_argument("--port", type=int, help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--data-dir", help=f"数据目录（默认 {DEFAULT_DATA_DIR}）")
    parser.add_argument("--bind-host",
                        help="绑定地址（默认 0.0.0.0；IPv6 用 '[::]' 或具体 IPv6）")
    parser.add_argument("--no-autostart", action="store_true",
                        help="不注册 macOS launchd 自启动")
    parser.add_argument("--init-only", action="store_true",
                        help="只初始化（写 self-signed 证书 + admin 密码），不启动服务")
    parser.add_argument("--print-admin-password", action="store_true",
                        help="打印当前 admin 密码到终端")
    return parser.parse_args(argv)


def load_or_init(data_dir: str) -> dict:
    """读 server.json；不存在则按默认初始化并写盘。"""
    os.makedirs(data_dir, exist_ok=True)
    cfg_path = os.path.join(data_dir, "server.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"读取 server.json 失败 ({e})，回退默认", file=sys.stderr)
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    else:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    # 补齐缺省
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v if not isinstance(v, (dict, list)) else (
            {**v} if isinstance(v, dict) else list(v)
        ))
    # 生成初始 admin 密码（首次启动）
    if not cfg.get("initial_admin", {}).get("password"):
        pwd = secrets.token_urlsafe(12)
        cfg["initial_admin"]["password"] = pwd
        _save(cfg_path, cfg)
        print(f"\n[初始化] 已生成 admin 账号密码：{pwd}")
        print(f"[初始化] 请立即登录 Web GUI（默认端口 {cfg['port']}）并修改。\n")
    else:
        # 配置已存在 + admin 密码已设 → 静默（不打印密码到终端）
        pass
    return cfg


def _save(cfg_path: str, cfg: dict) -> None:
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, cfg_path)


def save(cfg: dict) -> None:
    """把内存里的 cfg 写回 server.json（端口/密码等变更时调用）。"""
    cfg_path = os.path.join(cfg["data_dir"], "server.json")
    _save(cfg_path, cfg)