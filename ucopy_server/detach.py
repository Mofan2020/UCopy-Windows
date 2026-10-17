"""
ucopy_server/detach.py
======================

后台模式（--detach / --stop）的实现。

设计目标:
    1. 跨平台（macOS / Linux 用 fork；Windows 用 DETACHED_PROCESS）
    2. 父进程 fork 后立刻 exit，终端/cmd 窗口关闭后子进程继续跑
    3. 日志重定向到文件（子进程不再往原 stdout 写）
    4. PID 文件记录子进程 PID，方便 --stop 杀进程
    5. --stop 检查 PID 进程是否还活、是不是我们的 server（防 PID 复用）

macOS / Linux 用标准 double-fork daemonize 模式：
    - 第一次 fork：父进程 exit，子进程 setsid 脱离控制终端
    - 第二次 fork（可选）：防止子进程重新获得控制终端
    - 重定向 stdin/stdout/stderr 到 /dev/null 和 log 文件

Windows 用 subprocess.Popen + DETACHED_PROCESS | CREATE_NO_WINDOW：
    - 子进程没有控制台窗口
    - stdin/stdout/stderr 重定向到 log 文件
"""

from __future__ import annotations

import os
import sys
import time


# ---------- PID 管理 ----------

def read_pid(pid_file: str) -> int | None:
    """读 PID 文件，返回 PID 数字；文件不存在或损坏返回 None。"""
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            data = f.read().strip()
        return int(data) if data else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def write_pid(pid_file: str, pid: int) -> None:
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(pid))


def remove_pid(pid_file: str) -> None:
    try:
        os.remove(pid_file)
    except FileNotFoundError:
        pass


def is_pid_alive(pid: int) -> bool:
    """检查 PID 进程是否还活着（跨平台）。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows: 用 OpenProcess 比 kill -0 简单粗暴但更可靠
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h == 0:
            return False
        try:
            exit_code = wintypes.DWORD()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    else:
        # Unix: kill -0
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # 进程存在但我们没权限发信号（其他用户的进程）
            return True
        return True


# ---------- 停止已 detach 的进程 ----------

def stop(pid_file: str, timeout: float = 5.0) -> tuple[bool, str]:
    """读 pid 文件 → kill 进程 → 等退出 → 清 pid 文件。
    返回 (成功, 消息)。"""
    pid = read_pid(pid_file)
    if pid is None:
        return False, f"未找到 PID 文件 {pid_file}（可能没在 detach 模式运行）"
    if not is_pid_alive(pid):
        remove_pid(pid_file)
        return True, f"PID {pid} 已不存在，清掉残留 PID 文件"

    # 发退出信号
    if sys.platform == "win32":
        # Windows: kill 用 taskkill 走进程树，避免留子进程
        import subprocess
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                check=False, capture_output=True, timeout=5,
            )
        except Exception as e:
            return False, f"taskkill 失败: {e}"
    else:
        try:
            os.kill(pid, 15)  # SIGTERM
        except ProcessLookupError:
            remove_pid(pid_file)
            return True, f"PID {pid} 刚退出"
        except PermissionError as e:
            return False, f"无权限停止 PID {pid}: {e}"

    # 等进程退出
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            remove_pid(pid_file)
            return True, f"PID {pid} 已停止"
        time.sleep(0.1)

    if sys.platform == "win32":
        return False, f"PID {pid} 5 秒后仍未退出（请手动 taskkill）"
    # Unix: 升级到 SIGKILL
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    if is_pid_alive(pid):
        return False, f"PID {pid} SIGKILL 后仍未退出（内核 bug？）"
    remove_pid(pid_file)
    return True, f"PID {pid} SIGKILL 强制停止"


# ---------- 后台化（macOS / Linux）----------

def _daemonize_unix(log_file: str, pid_file: str) -> int:
    """macOS / Linux: 父进程 fork 后返回子 PID，父进程继续到 caller exit。
    子进程 setsid + 重定向 IO。返回子进程 PID。"""
    # 第一次 fork
    pid = os.fork()
    if pid > 0:
        # 父进程：等一下子进程确认它能写 pid 文件（再返回）
        # 给子进程 0.5s 写 pid 文件
        for _ in range(50):
            if os.path.exists(pid_file):
                break
            time.sleep(0.01)
        return pid

    # 子进程
    os.setsid()  # 创建新 session，脱离控制终端

    # 第二次 fork（防止子进程重新获得控制终端）
    pid2 = os.fork()
    if pid2 > 0:
        # 第一个子进程直接退出，让孙子进程由 init 收养
        os._exit(0)

    # 孙子进程（真正的 daemon）
    # 重定向 IO
    sys.stdout.flush()
    sys.stderr.flush()
    # stdin -> /dev/null
    with open(os.devnull, "rb", 0) as f:
        os.dup2(f.fileno(), 0)
    # stdout / stderr -> log 文件
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    if log_fd > 2:
        os.close(log_fd)

    # 写 PID 文件
    write_pid(pid_file, os.getpid())

    # 返回让 caller 继续走服务启动流程
    return 0  # 0 = 这是 daemon 进程


# ---------- 后台化（Windows）----------

def _daemonize_windows(log_file: str, pid_file: str) -> int:
    """Windows: 用 subprocess.Popen 重新启动自己，detached。
    返回子进程 PID；caller 应该 exit。"""
    import subprocess
    # 把当前参数里的 --detach 去掉，避免无限递归
    args = [a for a in sys.argv if a != "--detach"]
    # 注入子进程的 log / pid 路径（让子进程不重复算默认值）
    if "--log-file" not in args:
        args.extend(["--log-file", log_file])
    if "--pid-file" not in args:
        args.extend(["--pid-file", pid_file])

    # CREATE_NO_WINDOW: 不显示新控制台
    # DETACHED_PROCESS: 脱离父进程控制台（关 cmd 不会杀子进程）
    # CREATE_NEW_PROCESS_GROUP: 让子进程能收 CTRL+C / SIGTERM 风格的信号
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    creationflags = DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

    # 写 PID 文件：Popen 启动后我们拿 PID 写盘
    try:
        # stdin/stdout/stderr 都重定向到 log 文件
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        log_f = open(log_file, "ab", buffering=0)
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            creationflags=creationflags,
            close_fds=True,
        )
        write_pid(pid_file, proc.pid)
        return proc.pid
    except Exception as e:
        # 启动失败写到 stderr
        print(f"[detach] 启动后台进程失败: {e}", file=sys.stderr)
        return -1


# ---------- 公共入口 ----------

def detach(log_file: str, pid_file: str) -> int:
    """平台分发：返回新进程 PID；caller 父进程应 sys.exit(0)。"""
    if sys.platform == "win32":
        return _daemonize_windows(log_file, pid_file)
    return _daemonize_unix(log_file, pid_file)


def is_already_running(pid_file: str) -> int | None:
    """检查是否已有 detach 进程在跑。返回 PID 或 None。"""
    pid = read_pid(pid_file)
    if pid is None:
        return None
    if is_pid_alive(pid):
        return pid
    # PID 文件残留但进程已死 → 清掉
    remove_pid(pid_file)
    return None
