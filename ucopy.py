import os
import sys
import re
import json
import time
import hashlib
import secrets
import datetime
import threading
import logging
import logging.handlers
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import win32api
import win32con
import win32file
import win32gui
import win32gui_struct
import pythoncom
import pystray
from PIL import Image, ImageDraw

# ---------- 全局配置 ----------
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".ucopy_settings.json")

# 预设密保问题（用户可从中选择，也可保留自填入口）
SECURITY_QUESTIONS = [
    "您的小学校名是？",
    "您母亲的姓名是？",
    "您出生地的城市名是？",
    "您第一只宠物的名字是？",
    "您最喜欢的电影是？",
    "您小学最好朋友的名字是？",
    "您初中班主任的姓名是？",
    "您父亲的出生城市是？",
]

DEFAULT_CONFIG = {
    "target_folder": "",
    "max_total_files": 0,
    "max_total_size_mb": 0,
    "max_single_file_mb": 0,
    "min_free_space_mb": 100,
    "extensions": "",
    "regex_pattern": "",
    "auto_start": False,
    "known_devices": {},
    "security": {
        "password_hash": "",
        "password_salt": "",
        "qa": []  # [{"question": str, "answer_hash": str, "answer_salt": str}, ...]
    }
}

# ---------- 密码 / 密保答案哈希 ----------
def hash_secret(secret: str, salt: str = None):
    """
    对 secret（密码或密保答案）做 SHA-256 + 盐 哈希。
    返回 (salt, hash_hex)。salt 不传则随机生成 32 字节 hex。
    """
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + secret).encode("utf-8")).hexdigest()
    return salt, h

def verify_secret(secret: str, salt: str, expected_hash: str) -> bool:
    """校验 secret 与已存的 salt+hash 是否一致。"""
    if not salt or not expected_hash:
        return False
    _, h = hash_secret(secret, salt)
    return h == expected_hash

def normalize_answer(answer: str) -> str:
    """密保答案归一化：去首尾空格 + 转为小写（中文不受影响，但允许大小写不敏感匹配英文等）"""
    return answer.strip().lower()

def get_config():
    if not os.path.exists(CONFIG_PATH):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v if not isinstance(v, (dict, list)) else (
                    {**v} if isinstance(v, dict) else list(v)
                )
        # 进一步补齐嵌套字段
        sec = cfg.setdefault("security", {})
        for k, v in DEFAULT_CONFIG["security"].items():
            if k not in sec:
                sec[k] = v if not isinstance(v, (dict, list)) else (
                    {**v} if isinstance(v, dict) else list(v)
                )
        return cfg
    except:
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def set_auto_start(enable):
    key = win32api.RegOpenKey(
        win32con.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, win32con.KEY_SET_VALUE
    )
    if enable:
        if getattr(sys, 'frozen', False):
            exe_path = sys.executable
        else:
            exe_path = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
        win32api.RegSetValueEx(key, "UCopy", 0, win32con.REG_SZ, exe_path)
    else:
        try:
            win32api.RegDeleteValue(key, "UCopy")
        except:
            pass
    win32api.RegCloseKey(key)

def get_drive_info(drive_letter):
    """
    获取U盘的卷标和唯一标识符
    返回 (label, unique_id)
    优先使用GUID，失败则使用序列号+卷标组合
    """
    try:
        label, _, serial, _, _ = win32api.GetVolumeInformation(drive_letter + "\\")
        label = label.strip() if label else "无卷标"
    except:
        label = "未知磁盘"
        serial = 0

    # 尝试获取GUID路径
    guid = None
    try:
        # 必须传入 "X:\" 格式，注意结尾反斜杠
        vol_path = win32file.GetVolumeNameForVolumeMountPoint(drive_letter + "\\")
        # vol_path 格式: \\?\Volume{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}\
        vol_guid = vol_path.strip("\\").strip("\\").split("\\")[-1]
        if vol_guid.startswith("Volume{") and vol_guid.endswith("}"):
            guid = vol_guid[7:-1]  # 提取纯GUID字符串
    except Exception as e:
        logging.getLogger("UCopy").debug(f"获取GUID失败: {e}")

    if guid:
        unique_id = guid
    else:
        # 回退：使用序列号+卷标，为避免碰撞加上卷标hash
        unique_id = f"{serial:08X}_{label.replace(' ', '_')}"

    return label, unique_id

def get_free_space_mb(path):
    if not os.path.exists(path):
        p = path
        while p and not os.path.exists(p):
            p = os.path.dirname(p)
        if not p:
            return 0
        path = p
    free = win32file.GetDiskFreeSpaceEx(path)[0]
    return free // (1024 * 1024)

def get_total_files_and_size(root):
    total_files = 0
    total_size = 0
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(fp)
            except OSError:
                size = 0
            total_files += 1
            total_size += size
    return total_files, total_size

def should_copy_file(filename, extensions, pattern):
    if extensions.strip():
        exts = [e.strip().lower() for e in extensions.split(",") if e.strip()]
        if exts:
            _, ext = os.path.splitext(filename)
            if ext.lower() not in exts:
                return False
    if pattern.strip():
        try:
            if not re.search(pattern, filename):
                return False
        except re.error:
            return False
    return True

def copy_drive(drive_letter, config):
    """执行复制，返回 (success, message)"""
    logger = logging.getLogger("UCopy")
    target_base = config["target_folder"]
    if not target_base:
        return False, "未设置目标目录"

    label, unique_id = get_drive_info(drive_letter)
    dest_root = os.path.join(target_base, f"{label}_{unique_id}")
    src_root = drive_letter + "\\"

    logger.info(f"开始复制 U盘 [{label}] (ID: {unique_id})")
    logger.debug(f"源: {src_root}  目标: {dest_root}")

    # 目标空间检查
    min_free = config.get("min_free_space_mb", 0)
    if min_free > 0:
        check_path = target_base if os.path.exists(target_base) else os.path.dirname(target_base)
        free_mb = get_free_space_mb(check_path)
        if free_mb < min_free:
            msg = f"目标磁盘空间不足（剩余 {free_mb} MB，要求 {min_free} MB）"
            logger.error(msg)
            return False, msg

    # 总量检查
    total_files, total_size = get_total_files_and_size(src_root)
    logger.info(f"扫描完成：{total_files} 个文件，总大小 {total_size/(1024*1024):.2f} MB")
    max_files = config.get("max_total_files", 0)
    max_size_mb = config.get("max_total_size_mb", 0)

    if max_files > 0 and total_files > max_files:
        msg = f"文件数量超过限制（{total_files} > {max_files}）"
        logger.warning(msg)
        return False, msg
    if max_size_mb > 0 and total_size > max_size_mb * 1024 * 1024:
        msg = f"总大小超过限制（{total_size/(1024*1024):.1f} MB > {max_size_mb} MB）"
        logger.warning(msg)
        return False, msg

    max_single_mb = config.get("max_single_file_mb", 0)
    max_single_bytes = max_single_mb * 1024 * 1024 if max_single_mb > 0 else None
    ext_filter = config.get("extensions", "")
    regex_filter = config.get("regex_pattern", "")

    copied_count = 0
    skipped_count = 0

    try:
        for dirpath, _, filenames in os.walk(src_root):
            rel_dir = os.path.relpath(dirpath, src_root)
            dest_dir = os.path.join(dest_root, rel_dir) if rel_dir != "." else dest_root
            os.makedirs(dest_dir, exist_ok=True)

            for fn in filenames:
                src_file = os.path.join(dirpath, fn)
                try:
                    file_size = os.path.getsize(src_file)
                except OSError:
                    logger.warning(f"无法获取文件大小，跳过: {src_file}")
                    skipped_count += 1
                    continue

                # 单文件大小过滤
                if max_single_bytes and file_size > max_single_bytes:
                    logger.info(f"跳过(过大): {src_file} ({file_size/(1024*1024):.2f} MB)")
                    skipped_count += 1
                    continue

                # 扩展名/正则过滤
                if not should_copy_file(fn, ext_filter, regex_filter):
                    logger.info(f"跳过(过滤): {src_file}")
                    skipped_count += 1
                    continue

                dest_file = os.path.join(dest_dir, fn)
                temp_file = dest_file + ".ucopy"

                try:
                    win32file.CopyFile(src_file, temp_file, False)
                    # 保留时间戳
                    try:
                        st = os.stat(src_file)
                        os.utime(temp_file, (st.st_atime, st.st_mtime))
                    except:
                        pass
                    os.rename(temp_file, dest_file)
                    logger.info(f"复制成功: {src_file} -> {dest_file}")
                    copied_count += 1
                except Exception as e:
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass
                    logger.error(f"复制失败 ({e}): {src_file}")
                    skipped_count += 1
                    continue

        msg = f"复制完成：{copied_count} 个文件，跳过 {skipped_count} 个"
        logger.info(msg)
        return True, msg
    except Exception as e:
        msg = f"复制过程异常: {str(e)}"
        logger.exception(msg)
        return False, msg

# ---------- 全局日志系统（每2小时轮转）----------
class TimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """自定义轮转规则：每2小时生成新日志文件"""
    pass

def setup_logger(target_folder):
    """在目标目录下设置日志，每2小时轮转"""
    logger = logging.getLogger("UCopy")
    logger.setLevel(logging.DEBUG)

    # 避免重复添加handler
    if logger.handlers:
        return logger

    log_dir = target_folder if target_folder else os.path.expanduser("~")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "UCopy.log")

    # 使用 TimedRotatingFileHandler，每2小时轮转，保留30天
    handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="h", interval=2, backupCount=360, encoding="utf-8"
    )
    handler.suffix = "%Y%m%d_%H%M%S"
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # 同时输出到控制台（可选）
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    return logger

# ---------- 设备监控 ----------
class DeviceMonitor:
    def __init__(self, callback):
        self.callback = callback
        self.thread = None
        self.stop_event = threading.Event()

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_DEVICECHANGE:
            if wparam == win32con.DBT_DEVICEARRIVAL:
                try:
                    dev = win32gui_struct.UnpackDEV_BROADCAST(lparam)
                    if dev.devicetype == win32con.DBT_DEVTYP_VOLUME:
                        mask = dev.unitmask
                        for i in range(26):
                            if mask & (1 << i):
                                drive = chr(ord('A') + i) + ":"
                                if win32file.GetDriveType(drive + "\\") == win32con.DRIVE_REMOVABLE:
                                    self.callback(drive)
                except:
                    pass
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _run(self):
        pythoncom.CoInitialize()
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.lpszClassName = "UCopyMonitor"
        wc.hInstance = win32api.GetModuleHandle(None)
        atom = win32gui.RegisterClass(wc)
        hwnd = win32gui.CreateWindow(atom, "", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)
        while not self.stop_event.is_set():
            if win32gui.PumpWaitingMessages() != 0:
                break
        win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(atom, wc.hInstance)
        pythoncom.CoUninitialize()

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

# ---------- 主应用 ----------
class UCopyApp:
    def __init__(self):
        self.config = get_config()
        self.logger = None
        self.monitor = None
        self.tray_icon = None
        self.job_lock = threading.Lock()
        self.current_jobs = {}

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("UCopy")

        set_auto_start(self.config.get("auto_start", False))

    def init_logger(self):
        target = self.config.get("target_folder", "")
        self.logger = setup_logger(target)
        self.logger.info("UCopy 启动")
        self.logger.debug(f"配置文件路径: {CONFIG_PATH}")
        self.logger.debug(f"目标目录: {target if target else '未设置'}")

    def on_drive_inserted(self, drive_letter):
        self.logger.info(f"检测到U盘插入: {drive_letter}")
        time.sleep(1.5)  # 等待系统完全识别

        label, unique_id = get_drive_info(drive_letter)
        self.logger.info(f"U盘信息: 卷标={label}, 唯一ID={unique_id}")

        # 更新已知设备列表
        known = self.config.setdefault("known_devices", {})
        if unique_id not in known:
            known[unique_id] = {
                "label": label,
                "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "blacklisted": False
            }
            self.logger.info(f"新设备已记录: {label} ({unique_id})")
            save_config(self.config)
        else:
            # 更新标签（可能发生了变化）
            known[unique_id]["label"] = label
            save_config(self.config)

        # 检查黑名单
        if known[unique_id].get("blacklisted", False):
            self.logger.info(f"U盘 {label} 在黑名单中，跳过复制")
            return

        # 启动复制线程
        with self.job_lock:
            if drive_letter in self.current_jobs and self.current_jobs[drive_letter].is_alive():
                self.logger.info(f"该U盘已有复制任务正在运行，忽略重复插入")
                return
            t = threading.Thread(target=self.copy_job, args=(drive_letter,), daemon=True)
            self.current_jobs[drive_letter] = t
            t.start()

    def copy_job(self, drive_letter):
        success, msg = copy_drive(drive_letter, self.config)
        self.logger.info(f"复制任务结束 ({drive_letter}): {msg}")

    def start_monitor(self):
        if self.monitor is None:
            self.logger.info("设备监控已启动")
            self.monitor = DeviceMonitor(self.on_drive_inserted)
            self.monitor.start()

    def stop_monitor(self):
        if self.monitor:
            self.logger.info("设备监控已停止")
            self.monitor.stop()
            self.monitor = None

    def create_tray_icon(self):
        image = Image.new('RGB', (64, 64), color='white')
        dc = ImageDraw.Draw(image)
        dc.rectangle([16, 16, 48, 48], fill='blue')
        menu = pystray.Menu(
            pystray.MenuItem("打开设置", self.show_settings),
            pystray.MenuItem("退出", self.quit_app)
        )
        self.tray_icon = pystray.Icon("UCopy", image, "UCopy", menu)

    def show_settings(self, icon=None, item=None):
        """
        打开设置面板的统一入口：
          - 若未设置过密码，强制进入 SecuritySetupWindow
          - 若已设置，弹 LoginWindow 校验密码
          - 校验通过后回调进入 SettingsWindow
        """
        def _open_settings():
            SettingsWindow(self.root, self)

        sec = self.config.get("security", {})
        if not sec.get("password_hash"):
            SecuritySetupWindow(self.root, self, on_success=_open_settings)
        else:
            LoginWindow(self.root, self, on_success=_open_settings)

    def quit_app(self, icon=None, item=None):
        self.logger.info("用户请求退出程序")
        self.stop_monitor()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.init_logger()
        self.create_tray_icon()
        self.start_monitor()
        tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        tray_thread.start()
        self.root.after(500, self.show_settings)
        self.root.mainloop()

# ---------- 密码设置窗口（首次强制） ----------
class SecuritySetupWindow:
    """
    首次打开设置面板前强制要求：设置登录密码 + 3 个密保问题。
    关闭窗口必须完成设置（target=modal 阻塞）。
    """
    MIN_PASSWORD_LEN = 4
    QA_COUNT = 3

    def __init__(self, master, app, on_success):
        self.app = app
        self.on_success = on_success  # 回调：完成设置后进入 SettingsWindow
        self.win = tk.Toplevel(master)
        self.win.title("UCopy 安全设置")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._block_close)

        ttk.Label(
            self.win,
            text="首次使用，请设置登录密码与密保问题",
            font=("", 10, "bold")
        ).grid(row=0, column=0, columnspan=3, padx=10, pady=(10, 6), sticky="w")

        # ---- 密码 ----
        frm_pwd = ttk.LabelFrame(self.win, text="设置登录密码", padding=5)
        frm_pwd.grid(row=1, column=0, columnspan=3, padx=10, pady=5, sticky="ew")

        ttk.Label(frm_pwd, text=f"密码（至少 {self.MIN_PASSWORD_LEN} 位）:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.pwd_var = tk.StringVar()
        ttk.Entry(frm_pwd, textvariable=self.pwd_var, show="*", width=24).grid(row=0, column=1, padx=5)

        ttk.Label(frm_pwd, text="确认密码:").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.pwd2_var = tk.StringVar()
        ttk.Entry(frm_pwd, textvariable=self.pwd2_var, show="*", width=24).grid(row=1, column=1, padx=5)

        ttk.Label(frm_pwd, text="提示: 密码用于保护设置面板，请妥善保管。",
                  foreground="gray").grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=(2, 0))

        # ---- 密保 ----
        frm_qa = ttk.LabelFrame(self.win, text=f"密保问题（请设置 {self.QA_COUNT} 个，用于找回密码）", padding=5)
        frm_qa.grid(row=2, column=0, columnspan=3, padx=10, pady=5, sticky="ew")

        self.q_vars = []   # List[(question_var, answer_var)]
        for i in range(self.QA_COUNT):
            q_var = tk.StringVar(value=SECURITY_QUESTIONS[i])
            a_var = tk.StringVar()
            ttk.Label(frm_qa, text=f"问题 {i + 1}:").grid(row=i, column=0, sticky="w", padx=5, pady=2)
            q_combo = ttk.Combobox(frm_qa, textvariable=q_var, values=SECURITY_QUESTIONS,
                                   width=30, state="normal")
            q_combo.grid(row=i, column=1, padx=5, pady=2)
            ttk.Label(frm_qa, text="答案:").grid(row=i, column=2, sticky="w", padx=5, pady=2)
            ttk.Entry(frm_qa, textvariable=a_var, show="*", width=20).grid(row=i, column=3, padx=5, pady=2)
            self.q_vars.append((q_var, a_var))

        ttk.Label(frm_qa, text="提示: 答案不区分大小写，请设置您能记住的固定答案。",
                  foreground="gray").grid(row=self.QA_COUNT, column=0, columnspan=4,
                                            sticky="w", padx=5, pady=(2, 0))

        # ---- 提交 ----
        btn_frame = ttk.Frame(self.win)
        btn_frame.grid(row=3, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="保存并进入设置", command=self.save).pack(side=tk.LEFT, padx=5)

        self.win.grab_set()

    def _block_close(self):
        messagebox.showwarning("必须设置", "请先完成密码与密保设置，才能使用本程序。", parent=self.win)

    def save(self):
        pwd = self.pwd_var.get()
        pwd2 = self.pwd2_var.get()
        if len(pwd) < self.MIN_PASSWORD_LEN:
            messagebox.showerror("错误", f"密码长度不能少于 {self.MIN_PASSWORD_LEN} 位", parent=self.win)
            return
        if pwd != pwd2:
            messagebox.showerror("错误", "两次输入的密码不一致", parent=self.win)
            return

        # 收集密保
        qa = []
        for i, (q_var, a_var) in enumerate(self.q_vars):
            q = q_var.get().strip()
            a = a_var.get()
            if not q:
                messagebox.showerror("错误", f"第 {i + 1} 个密保问题不能为空", parent=self.win)
                return
            if not a.strip():
                messagebox.showerror("错误", f"第 {i + 1} 个密保答案不能为空", parent=self.win)
                return
            salt, h = hash_secret(normalize_answer(a))
            qa.append({"question": q, "answer_hash": h, "answer_salt": salt})

        pwd_salt, pwd_hash = hash_secret(pwd)
        self.app.config["security"] = {
            "password_hash": pwd_hash,
            "password_salt": pwd_salt,
            "qa": qa
        }
        save_config(self.app.config)
        try:
            self.app.logger.info("安全设置已初始化（密码 + 密保问题）")
        except Exception:
            pass
        self.win.destroy()
        if self.on_success:
            self.on_success()


# ---------- 登录窗口（每次进入设置前） ----------
class LoginWindow:
    """
    校验登录密码。校验通过后回调 on_success 进入 SettingsWindow。
    忘记密码入口：跳到 ForgotPasswordWindow，走密保问题校验后重置密码。
    """
    MAX_ATTEMPTS = 5  # 失败超过次数强制退出

    def __init__(self, master, app, on_success):
        self.app = app
        self.on_success = on_success
        self.attempts = 0
        self._locked = False  # 触顶后置 True，幂等保护
        self.win = tk.Toplevel(master)
        self.win.title("UCopy - 验证密码")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel_and_quit)

        ttk.Label(self.win, text="请输入登录密码以进入设置面板",
                  font=("", 10)).grid(row=0, column=0, columnspan=2, padx=20, pady=(15, 6))

        self.pwd_var = tk.StringVar()
        entry = ttk.Entry(self.win, textvariable=self.pwd_var, show="*", width=28)
        entry.grid(row=1, column=0, columnspan=2, padx=20, pady=5)
        entry.focus_set()

        self.err_var = tk.StringVar(value="")
        ttk.Label(self.win, textvariable=self.err_var, foreground="red").grid(
            row=2, column=0, columnspan=2, padx=20, pady=(0, 5))

        btn_frame = ttk.Frame(self.win)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="确定", command=self.verify).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="忘记密码", command=self.forgot).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.cancel_and_quit).pack(side=tk.LEFT, padx=5)

        self.win.bind("<Return>", lambda e: self.verify())
        self.win.grab_set()

    def cancel_and_quit(self):
        """取消登录：直接退出整个应用（密码保护下不应让用户绕过）"""
        self.win.destroy()
        self.app.quit_app()

    def verify(self):
        if self._locked:
            return
        sec = self.app.config.get("security", {})
        salt = sec.get("password_salt", "")
        expected = sec.get("password_hash", "")
        if verify_secret(self.pwd_var.get(), salt, expected):
            try:
                self.app.logger.info("设置面板密码验证通过")
            except Exception:
                pass
            self.win.destroy()
            if self.on_success:
                self.on_success()
            return

        self.attempts += 1
        left = self.MAX_ATTEMPTS - self.attempts
        try:
            self.app.logger.warning(
                f"设置面板密码错误（第 {self.attempts}/{self.MAX_ATTEMPTS} 次）"
            )
        except Exception:
            pass
        if left <= 0:
            # 达到上限：仅关闭密码窗口，程序与后台 U 盘复制继续运行，
            # 留下审计日志供事后追溯。需要再试可从托盘重新打开设置。
            try:
                self.app.logger.warning(
                    f"设置面板密码连续 {self.MAX_ATTEMPTS} 次错误，临时锁定，"
                    "U 盘复制功能未受影响"
                )
            except Exception:
                pass
            self._locked = True
            self.win.destroy()
            messagebox.showerror(
                "已临时锁定",
                f"密码连续 {self.MAX_ATTEMPTS} 次错误，已关闭设置面板。\n"
                "后台 U 盘复制功能继续运行，需要时可从托盘菜单重新打开设置。",
                parent=self.app.root,
            )
            return
        self.err_var.set(f"密码错误，还可尝试 {left} 次")
        self.pwd_var.set("")

    def forgot(self):
        if self._locked:
            return
        # 进入密保找回流程
        sec = self.app.config.get("security", {})
        qa = sec.get("qa", [])
        if not qa:
            messagebox.showerror("错误", "未配置密保问题，无法找回密码", parent=self.win)
            return
        self.win.withdraw()
        ForgotPasswordWindow(self.win, self.app, qa, on_reset=self._on_reset_done)

    def _on_reset_done(self):
        """密保校验通过、重置密码后，回到登录窗口（让用户用新密码再登一次）"""
        # 直接进入设置
        if self.on_success:
            self.on_success()
        self.win.destroy()


class ForgotPasswordWindow:
    """
    密保找回流程：依次回答所有密保问题，全部答对才能重置密码。
    """
    def __init__(self, master, app, qa, on_reset):
        self.app = app
        self.qa = qa
        self.on_reset = on_reset  # 重置完成回调
        self.win = tk.Toplevel(master)
        self.win.title("找回密码")
        self.win.resizable(False, False)
        self.win.grab_set()

        ttk.Label(
            self.win,
            text="请依次回答以下密保问题，全部答对后可重置密码",
            font=("", 10)
        ).grid(row=0, column=0, columnspan=2, padx=15, pady=(10, 5), sticky="w")

        self.answer_vars = []
        for i, item in enumerate(qa):
            ttk.Label(self.win, text=f"{i + 1}. {item['question']}").grid(
                row=1 + i, column=0, sticky="w", padx=10, pady=3)
            a_var = tk.StringVar()
            ttk.Entry(self.win, textvariable=a_var, show="*", width=24).grid(
                row=1 + i, column=1, padx=10, pady=3)
            self.answer_vars.append(a_var)

        self.err_var = tk.StringVar(value="")
        ttk.Label(self.win, textvariable=self.err_var, foreground="red").grid(
            row=1 + len(qa), column=0, columnspan=2, padx=10, pady=(2, 4))

        btn_frame = ttk.Frame(self.win)
        btn_frame.grid(row=2 + len(qa), column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="下一步", command=self.check_answers).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.cancel).pack(side=tk.LEFT, padx=5)

    def check_answers(self):
        wrong = []
        for i, (item, a_var) in enumerate(zip(self.qa, self.answer_vars)):
            ans = a_var.get()
            if not verify_secret(normalize_answer(ans), item["answer_salt"], item["answer_hash"]):
                wrong.append(i + 1)
        if wrong:
            self.err_var.set(f"第 {', '.join(map(str, wrong))} 题答案错误，请重试")
            return
        # 全部答对，进入重置密码
        self.win.destroy()
        ResetPasswordWindow(self.win.master, self.app, after_reset=self.on_reset)

    def cancel(self):
        self.win.destroy()
        # 把登录窗口恢复显示
        try:
            self.win.master.deiconify()
        except Exception:
            pass


class ResetPasswordWindow:
    """通过密保后，重置新密码"""
    MIN_PASSWORD_LEN = SecuritySetupWindow.MIN_PASSWORD_LEN

    def __init__(self, master, app, after_reset):
        self.app = app
        self.after_reset = after_reset
        self.win = tk.Toplevel(master)
        self.win.title("重置密码")
        self.win.resizable(False, False)
        self.win.grab_set()

        ttk.Label(self.win, text="密保校验通过，请设置新密码",
                  font=("", 10)).grid(row=0, column=0, columnspan=2, padx=20, pady=(12, 6))

        ttk.Label(self.win, text=f"新密码（至少 {self.MIN_PASSWORD_LEN} 位）:").grid(
            row=1, column=0, sticky="w", padx=10, pady=4)
        self.pwd_var = tk.StringVar()
        ttk.Entry(self.win, textvariable=self.pwd_var, show="*", width=22).grid(
            row=1, column=1, padx=10, pady=4)

        ttk.Label(self.win, text="确认新密码:").grid(
            row=2, column=0, sticky="w", padx=10, pady=4)
        self.pwd2_var = tk.StringVar()
        ttk.Entry(self.win, textvariable=self.pwd2_var, show="*", width=22).grid(
            row=2, column=1, padx=10, pady=4)

        btn_frame = ttk.Frame(self.win)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="重置", command=self.save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.cancel).pack(side=tk.LEFT, padx=5)

    def save(self):
        pwd = self.pwd_var.get()
        pwd2 = self.pwd2_var.get()
        if len(pwd) < self.MIN_PASSWORD_LEN:
            messagebox.showerror("错误", f"密码长度不能少于 {self.MIN_PASSWORD_LEN} 位", parent=self.win)
            return
        if pwd != pwd2:
            messagebox.showerror("错误", "两次输入的密码不一致", parent=self.win)
            return
        salt, h = hash_secret(pwd)
        sec = self.app.config.setdefault("security", {})
        sec["password_salt"] = salt
        sec["password_hash"] = h
        save_config(self.app.config)
        try:
            self.app.logger.info("密码已通过密保重置")
        except Exception:
            pass
        messagebox.showinfo("完成", "密码已重置", parent=self.win)
        self.win.destroy()
        if self.after_reset:
            self.after_reset()

    def cancel(self):
        self.win.destroy()


# ---------- 修改密码 / 修改密保（设置面板内入口） ----------
class ChangePasswordWindow:
    """在设置面板内修改密码：需先输入旧密码"""
    MIN_PASSWORD_LEN = SecuritySetupWindow.MIN_PASSWORD_LEN

    def __init__(self, master, app):
        self.app = app
        self.win = tk.Toplevel(master)
        self.win.title("修改密码")
        self.win.resizable(False, False)
        self.win.grab_set()

        ttk.Label(self.win, text="修改登录密码", font=("", 10, "bold")).grid(
            row=0, column=0, columnspan=2, padx=20, pady=(10, 6))

        ttk.Label(self.win, text="当前密码:").grid(row=1, column=0, sticky="w", padx=10, pady=4)
        self.old_var = tk.StringVar()
        ttk.Entry(self.win, textvariable=self.old_var, show="*", width=22).grid(
            row=1, column=1, padx=10, pady=4)

        ttk.Label(self.win, text=f"新密码（至少 {self.MIN_PASSWORD_LEN} 位）:").grid(
            row=2, column=0, sticky="w", padx=10, pady=4)
        self.new_var = tk.StringVar()
        ttk.Entry(self.win, textvariable=self.new_var, show="*", width=22).grid(
            row=2, column=1, padx=10, pady=4)

        ttk.Label(self.win, text="确认新密码:").grid(row=3, column=0, sticky="w", padx=10, pady=4)
        self.new2_var = tk.StringVar()
        ttk.Entry(self.win, textvariable=self.new2_var, show="*", width=22).grid(
            row=3, column=1, padx=10, pady=4)

        self.err_var = tk.StringVar(value="")
        ttk.Label(self.win, textvariable=self.err_var, foreground="red").grid(
            row=4, column=0, columnspan=2, padx=10, pady=(2, 4))

        btn_frame = ttk.Frame(self.win)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="保存", command=self.save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.win.destroy).pack(side=tk.LEFT, padx=5)

    def save(self):
        sec = self.app.config.get("security", {})
        if not verify_secret(self.old_var.get(), sec.get("password_salt", ""), sec.get("password_hash", "")):
            self.err_var.set("当前密码错误")
            return
        new = self.new_var.get()
        if len(new) < self.MIN_PASSWORD_LEN:
            self.err_var.set(f"新密码长度不能少于 {self.MIN_PASSWORD_LEN} 位")
            return
        if new != self.new2_var.get():
            self.err_var.set("两次输入的新密码不一致")
            return
        salt, h = hash_secret(new)
        sec["password_salt"] = salt
        sec["password_hash"] = h
        self.app.config["security"] = sec
        save_config(self.app.config)
        try:
            self.app.logger.info("登录密码已修改")
        except Exception:
            pass
        messagebox.showinfo("完成", "密码已修改", parent=self.win)
        self.win.destroy()


class ChangeQAWindow:
    """在设置面板内修改密保问题：需先校验当前密码"""
    QA_COUNT = SecuritySetupWindow.QA_COUNT

    def __init__(self, master, app):
        self.app = app
        self.win = tk.Toplevel(master)
        self.win.title("修改密保问题")
        self.win.resizable(False, False)
        self.win.grab_set()

        ttk.Label(self.win, text=f"修改密保问题（{self.QA_COUNT} 个）", font=("", 10, "bold")).grid(
            row=0, column=0, columnspan=4, padx=10, pady=(10, 6))

        ttk.Label(self.win, text="请先输入当前密码:").grid(row=1, column=0, sticky="w", padx=5, pady=4)
        self.pwd_var = tk.StringVar()
        ttk.Entry(self.win, textvariable=self.pwd_var, show="*", width=20).grid(
            row=1, column=1, padx=5, pady=4)

        # 复用现有密保问题为默认值
        existing_qa = app.config.get("security", {}).get("qa", [])
        default_questions = (
            [item.get("question", SECURITY_QUESTIONS[i]) for i, item in enumerate(existing_qa)]
            + SECURITY_QUESTIONS
        )[: max(self.QA_COUNT, len(existing_qa))]

        self.q_vars = []
        for i in range(self.QA_COUNT):
            ttk.Label(self.win, text=f"问题 {i + 1}:").grid(row=2 + i, column=0, sticky="w", padx=5, pady=2)
            q_var = tk.StringVar(value=default_questions[i] if i < len(default_questions) else SECURITY_QUESTIONS[i])
            ttk.Combobox(self.win, textvariable=q_var, values=SECURITY_QUESTIONS,
                         width=30, state="normal").grid(row=2 + i, column=1, padx=5, pady=2)
            ttk.Label(self.win, text="新答案:").grid(row=2 + i, column=2, sticky="w", padx=5, pady=2)
            a_var = tk.StringVar()
            ttk.Entry(self.win, textvariable=a_var, show="*", width=20).grid(row=2 + i, column=3, padx=5, pady=2)
            self.q_vars.append((q_var, a_var))

        self.err_var = tk.StringVar(value="")
        ttk.Label(self.win, textvariable=self.err_var, foreground="red").grid(
            row=2 + self.QA_COUNT, column=0, columnspan=4, padx=5, pady=(2, 4))

        btn_frame = ttk.Frame(self.win)
        btn_frame.grid(row=3 + self.QA_COUNT, column=0, columnspan=4, pady=8)
        ttk.Button(btn_frame, text="保存", command=self.save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="取消", command=self.win.destroy).pack(side=tk.LEFT, padx=5)

    def save(self):
        sec = self.app.config.get("security", {})
        if not verify_secret(self.pwd_var.get(), sec.get("password_salt", ""), sec.get("password_hash", "")):
            self.err_var.set("当前密码错误")
            return
        qa = []
        for i, (q_var, a_var) in enumerate(self.q_vars):
            q = q_var.get().strip()
            a = a_var.get()
            if not q:
                self.err_var.set(f"第 {i + 1} 个问题不能为空")
                return
            if not a.strip():
                self.err_var.set(f"第 {i + 1} 个答案不能为空")
                return
            salt, h = hash_secret(normalize_answer(a))
            qa.append({"question": q, "answer_hash": h, "answer_salt": salt})
        sec["qa"] = qa
        self.app.config["security"] = sec
        save_config(self.app.config)
        try:
            self.app.logger.info("密保问题已更新")
        except Exception:
            pass
        messagebox.showinfo("完成", "密保问题已更新", parent=self.win)
        self.win.destroy()


# ---------- 设置窗口 ----------
class SettingsWindow:
    def __init__(self, master, app):
        self.app = app
        self.win = tk.Toplevel(master)
        self.win.title("UCopy 设置")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)

        cfg = app.config

        # 基本设置
        frame_basic = ttk.LabelFrame(self.win, text="基本设置", padding=5)
        frame_basic.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="ew")

        ttk.Label(frame_basic, text="目标目录 (必填):").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.target_var = tk.StringVar(value=cfg["target_folder"])
        ttk.Entry(frame_basic, textvariable=self.target_var, width=45).grid(row=0, column=1, padx=5, pady=2)
        ttk.Button(frame_basic, text="浏览", command=self.browse_target).grid(row=0, column=2, padx=5)

        ttk.Label(frame_basic, text="最大文件数 (0=不限):").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.max_files_var = tk.IntVar(value=cfg["max_total_files"])
        ttk.Entry(frame_basic, textvariable=self.max_files_var, width=10).grid(row=1, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="最大总大小(MB) (0=不限):").grid(row=2, column=0, sticky="w", padx=5, pady=2)
        self.max_size_var = tk.IntVar(value=cfg["max_total_size_mb"])
        ttk.Entry(frame_basic, textvariable=self.max_size_var, width=10).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="跳过单文件大于(MB) (0=不限):").grid(row=3, column=0, sticky="w", padx=5, pady=2)
        self.single_var = tk.IntVar(value=cfg["max_single_file_mb"])
        ttk.Entry(frame_basic, textvariable=self.single_var, width=10).grid(row=3, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="目标空间不足(MB)不复制:").grid(row=4, column=0, sticky="w", padx=5, pady=2)
        self.space_var = tk.IntVar(value=cfg["min_free_space_mb"])
        ttk.Entry(frame_basic, textvariable=self.space_var, width=10).grid(row=4, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="扩展名 (如 .jpg,.png):").grid(row=5, column=0, sticky="w", padx=5, pady=2)
        self.ext_var = tk.StringVar(value=cfg["extensions"])
        ttk.Entry(frame_basic, textvariable=self.ext_var, width=20).grid(row=5, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="文件名正则 (可空):").grid(row=6, column=0, sticky="w", padx=5, pady=2)
        self.regex_var = tk.StringVar(value=cfg["regex_pattern"])
        ttk.Entry(frame_basic, textvariable=self.regex_var, width=20).grid(row=6, column=1, sticky="w", padx=5)

        self.auto_var = tk.BooleanVar(value=cfg["auto_start"])
        ttk.Checkbutton(frame_basic, text="开机自动启动", variable=self.auto_var).grid(row=7, column=0, columnspan=2, sticky="w", padx=5, pady=5)

        # 黑名单管理
        frame_black = ttk.LabelFrame(self.win, text="已识别U盘 (勾选后不再复制)", padding=5)
        frame_black.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")

        self.device_listbox = tk.Listbox(frame_black, width=60, height=6, selectmode=tk.MULTIPLE)
        self.device_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(frame_black, orient=tk.VERTICAL, command=self.device_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.device_listbox.config(yscrollcommand=scrollbar.set)

        self.refresh_device_list()

        btn_frame_black = ttk.Frame(self.win)
        btn_frame_black.grid(row=2, column=0, columnspan=2, pady=5)
        ttk.Button(btn_frame_black, text="切换黑名单状态", command=self.toggle_blacklist).pack(side=tk.LEFT, padx=5)

        # 底部按钮
        btn_frame = ttk.Frame(self.win)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="保存并隐藏", command=self.save_and_hide).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="修改密码", command=self.change_password).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="修改密保", command=self.change_qa).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="退出程序", command=self.app.quit_app).pack(side=tk.LEFT, padx=5)

        self.win.grab_set()

    def browse_target(self):
        folder = filedialog.askdirectory(parent=self.win)
        if folder:
            self.target_var.set(folder)

    def refresh_device_list(self):
        self.device_listbox.delete(0, tk.END)
        known = self.app.config.get("known_devices", {})
        for uid, info in known.items():
            label = info.get("label", "?")
            black = info.get("blacklisted", False)
            display = f"{label}  [{uid}]  {'[黑名单]' if black else '[正常]'}"
            self.device_listbox.insert(tk.END, display)

    def toggle_blacklist(self):
        selected = self.device_listbox.curselection()
        if not selected:
            messagebox.showinfo("提示", "请先选择U盘")
            return
        known = self.app.config.get("known_devices", {})
        items = list(known.items())
        for idx in selected:
            if idx < len(items):
                uid, info = items[idx]
                info["blacklisted"] = not info.get("blacklisted", False)
        save_config(self.app.config)
        self.refresh_device_list()

    def save_and_hide(self):
        target = self.target_var.get().strip()
        if not target:
            messagebox.showerror("错误", "目标目录不能为空", parent=self.win)
            return
        # 如果目标目录变更，需要更新日志路径
        old_target = self.app.config.get("target_folder", "")
        if target != old_target:
            self.app.logger.info(f"目标目录已更改: {old_target} -> {target}")
            # 重新配置日志
            new_logger = setup_logger(target)
            for handler in self.app.logger.handlers[:]:
                self.app.logger.removeHandler(handler)
            for handler in new_logger.handlers:
                self.app.logger.addHandler(handler)

        # 保留 self.app.config 中 UI 未编辑的字段（security、known_devices 等），
        # 仅覆盖当前面板上修改过的字段。
        # 注意：以前这里显式列字段，结果漏掉了 security，每次"保存并隐藏"
        # 都会把磁盘上的密码/密保擦成默认值，导致重启后又要重设。
        cfg = {
            **self.app.config,
            "target_folder": target,
            "max_total_files": self.max_files_var.get(),
            "max_total_size_mb": self.max_size_var.get(),
            "max_single_file_mb": self.single_var.get(),
            "min_free_space_mb": self.space_var.get(),
            "extensions": self.ext_var.get().strip(),
            "regex_pattern": self.regex_var.get().strip(),
            "auto_start": self.auto_var.get(),
        }
        save_config(cfg)
        self.app.config = cfg
        set_auto_start(cfg["auto_start"])
        self.app.logger.info("设置已保存")
        self.win.destroy()

    def on_close(self):
        self.win.destroy()

    def change_password(self):
        ChangePasswordWindow(self.win, self.app)

    def change_qa(self):
        ChangeQAWindow(self.win, self.app)

if __name__ == "__main__":
    app = UCopyApp()
    app.run()