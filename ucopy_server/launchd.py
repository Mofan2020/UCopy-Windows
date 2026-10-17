"""
ucopy-server/launchd.py
=======================

macOS 专属：通过 ~/Library/LaunchAgents/com.skyc8266.ucopy-server.plist 注册自启动。

行为：
    is_installed()  -> bool     plist 是否已创建
    install(exec_cmd)           写 plist + launchctl load
    uninstall()                 launchctl unload + 删 plist
    get_status()   -> "running" | "installed" | "not_installed"

Linux 上所有调用都是 no-op（返回未安装 / 跳过）。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

LABEL = "com.skyc8266.ucopy-server"


def plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def is_installed() -> bool:
    return os.path.exists(plist_path())


def get_status() -> str:
    if not is_installed():
        return "not_installed"
    if not _launchctl("list", LABEL, check=False):
        return "installed"  # plist 在，但没运行
    return "running"


def install(args) -> bool:
    """
    写 plist + launchctl load -w

    args: 命令参数列表，例如
        ["/usr/bin/python3", "/Users/.../ucopy-server/server.py", "--detach"]
    或直接传字符串（兼容旧用法：exec_cmd = "/usr/bin/python3 /path/server.py"）
    """
    if platform.system() != "Darwin":
        print("launchd 自启动仅 macOS 支持", file=sys.stderr)
        return False
    plist_dir = os.path.dirname(plist_path())
    os.makedirs(plist_dir, exist_ok=True)

    # 兼容旧调用：exec_cmd 字符串
    if isinstance(args, str):
        # 旧用法：把字符串当成一个 ProgramArguments 项（用 /bin/sh -c 包装）
        program_args = ["/bin/sh", "-c", args]
    else:
        program_args = list(args)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{chr(10).join(f'    <string>{_xml_escape(a)}</string>' for a in program_args)}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>~/Library/Logs/{LABEL}.out.log</string>
  <key>StandardErrorPath</key><string>~/Library/Logs/{LABEL}.err.log</string>
</dict>
</plist>
"""
    with open(plist_path(), "w", encoding="utf-8") as f:
        f.write(plist)
    os.chmod(plist_path(), 0o644)

    # load
    rc = _launchctl("load", "-w", plist_path(), check=False)
    return rc == 0


def uninstall() -> bool:
    if platform.system() != "Darwin":
        return False
    if is_installed():
        _launchctl("unload", "-w", plist_path(), check=False)
        try:
            os.remove(plist_path())
        except OSError:
            pass
    return not is_installed()


def _launchctl(*args, check=True):
    if platform.system() != "Darwin":
        return 0
    launchctl = shutil.which("launchctl")
    if not launchctl:
        return 1 if check else 0
    cmd = [launchctl, *args]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return 0
    except subprocess.CalledProcessError as e:
        if check:
            print(f"launchctl 失败 ({e.returncode}): {e.output.decode(errors='replace')}",
                  file=sys.stderr)
        return e.returncode
    except FileNotFoundError:
        return 1 if check else 0


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))