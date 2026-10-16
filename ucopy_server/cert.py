"""
ucopy-server/cert.py
====================

自签证书生成（首次启动时）。

调用系统 openssl（macOS / Linux 自带）。Windows 下需要先装 openssl 或换实现 —— 本项目
服务器目标平台是 macOS / Linux（Web GUI + launchd 自启动都是 macOS 专属，但 Linux 上也
能正常跑，只是 launchd 那段是 no-op）。

证书：
    - RSA 2048
    - 365 天有效
    - CN = "UCopy-Server (self-signed)"
    - SAN = 用户传入的 IP（可空；缺省只填 localhost）

输出两个文件：
    <data_dir>/cert.pem
    <data_dir>/key.pem
"""

from __future__ import annotations

import datetime
import os
import shutil
import socket
import subprocess
import sys


def detect_local_ips() -> list:
    """返回本机所有可达 IPv4 / IPv6 地址（link-local 过滤）。"""
    ips = set()
    # 方法1：getaddrinfo
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None)
        for info in infos:
            sockaddr = info[4]
            addr = sockaddr[0] if sockaddr else None
            if not isinstance(addr, str):
                continue
            if ":" in addr and not addr.startswith("fe80"):
                ips.add(addr)
            elif "." in addr and not addr.startswith("127."):
                ips.add(addr)
    except Exception:
        pass
    # 方法2：udp socket connect
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return sorted(ips)


def ensure_cert(data_dir: str, extra_san_ips=None) -> tuple:
    """
    确保 <data_dir>/cert.pem + key.pem 存在；不存在则生成。
    已存在且未过期则原样返回。
    返回 (cert_path, key_path)。
    """
    cert_path = os.path.join(data_dir, "cert.pem")
    key_path = os.path.join(data_dir, "key.pem")

    if os.path.exists(cert_path) and os.path.exists(key_path):
        # 检查过期（7 天内过期则续签）
            # openssl x509 -enddate 解析
            try:
                out = subprocess.check_output(
                    ["openssl", "x509", "-enddate", "-noout",
                     "-in", cert_path],
                    stderr=subprocess.DEVNULL,
                ).decode().strip()
                # 格式: notAfter=Oct 15 12:00:00 2026 GMT
                date_str = out.split("=", 1)[1]
                end = datetime.datetime.strptime(
                    date_str, "%b %d %H:%M:%S %Y %Z"
                ).replace(tzinfo=datetime.timezone.utc)
                now = datetime.datetime.now(datetime.timezone.utc)
                if end - now > datetime.timedelta(days=7):
                    return cert_path, key_path
                # 否则续签
            except Exception:
                # openssl 不可用 / 解析失败 → 跳过检查，假定现证书可用
                return cert_path, key_path

    openssl = shutil.which("openssl")
    if not openssl:
        print("错误：找不到 openssl，无法生成自签证书", file=sys.stderr)
        print("  macOS 自带；Linux 大多数发行版自带；Windows 请装 Git for Windows", file=sys.stderr)
        sys.exit(1)

    san_entries = ["DNS:localhost", "IP:127.0.0.1"]
    local_ips = detect_local_ips()
    if extra_san_ips:
        local_ips = list(set(local_ips) | set(extra_san_ips))
    for ip in local_ips:
        if ":" in ip:
            san_entries.append(f"IP:{ip}")
        elif "." in ip:
            san_entries.append(f"IP:{ip}")
    san_str = ",".join(san_entries)

    cmd = [
        openssl, "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", key_path, "-out", cert_path,
        "-days", "365", "-nodes",
        "-subj", "/CN=UCopy-Server (self-signed)",
        "-addext", f"subjectAltName={san_str}",
    ]
    print(f"[证书] 生成自签证书: SAN={san_str}")
    try:
        subprocess.check_call(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        print(f"[证书] openssl 失败: {e}", file=sys.stderr)
        sys.exit(1)
    return cert_path, key_path