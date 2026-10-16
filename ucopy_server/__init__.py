"""
UCopy 远程模式配套服务器 — 子模块入口。

目录布局：
    ucopy_server/
        __init__.py    # 本文件
        server.py      # HTTPS + REST + Web GUI 主程序
        auth.py        # PBKDF2 密码哈希 + session token
        db.py          # SQLite (users / config / files) 数据访问
        storage.py     # 用户文件存储 + 配额统计
        launchd.py     # macOS 自启动管理（launchd plist 生成 + load/unload）
        cert.py        # self-signed 证书生成（首次启动自动）
        config.py      # 服务器配置（监听端口、数据目录、初始管理员）

依赖：仅 Python 3.9+ 标准库。无 Flask / 无 requests / 无 cryptography。
启动：
    python3 ucopy_server/server.py

默认：
    监听 https://[::]:8443  （双栈，IPv4 + IPv6）
    数据目录 ~/UCopyServer/
    首次启动会创建 self-signed 证书 + 初始管理员账号 (admin/<随机密码>)
"""

__version__ = "1.0.0"
__all__ = ["server", "auth", "db", "storage", "launchd", "cert", "config"]