import os
import sys
import re
import json
import time
import threading
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

# ---------- 全局配置存储路径 ----------
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".ucopy_settings.json")

DEFAULT_CONFIG = {
    "target_folder": "",
    "max_total_files": 0,
    "max_total_size_mb": 0,
    "max_single_file_mb": 0,
    "min_free_space_mb": 100,
    "extensions": "",
    "regex_pattern": "",
    "auto_start": False
}

# ---------- 工具函数 ----------
def get_config():
    if not os.path.exists(CONFIG_PATH):
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    except:
        return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def set_auto_start(enable):
    """通过注册表实现开机自启"""
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
    try:
        label, _, serial, _, _ = win32api.GetVolumeInformation(drive_letter + "\\")
        return label or "无卷标", f"{serial:08X}"
    except:
        return "未知磁盘", "00000000"

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
    target_base = config["target_folder"]
    if not target_base:
        return False, "未设置目标目录"

    label, serial = get_drive_info(drive_letter)
    dest_root = os.path.join(target_base, f"{label}_{serial}")
    src_root = drive_letter + "\\"

    min_free = config.get("min_free_space_mb", 0)
    if min_free > 0:
        check_path = target_base if os.path.exists(target_base) else os.path.dirname(target_base)
        free_mb = get_free_space_mb(check_path)
        if free_mb < min_free:
            return False, f"目标磁盘空间不足（剩余 {free_mb} MB，要求 {min_free} MB）"

    total_files, total_size = get_total_files_and_size(src_root)
    max_files = config.get("max_total_files", 0)
    max_size_mb = config.get("max_total_size_mb", 0)

    if max_files > 0 and total_files > max_files:
        return False, f"文件数量超过限制（{total_files} > {max_files}）"
    if max_size_mb > 0 and total_size > max_size_mb * 1024 * 1024:
        return False, f"总大小超过限制（{total_size / (1024*1024):.1f} MB > {max_size_mb} MB）"

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
                    skipped_count += 1
                    continue

                if max_single_bytes and file_size > max_single_bytes:
                    skipped_count += 1
                    continue

                if not should_copy_file(fn, ext_filter, regex_filter):
                    skipped_count += 1
                    continue

                dest_file = os.path.join(dest_dir, fn)
                try:
                    win32file.CopyFile(src_file, dest_file, False)
                except Exception as e:
                    skipped_count += 1
                    continue
                # 保留时间戳
                try:
                    st = os.stat(src_file)
                    os.utime(dest_file, (st.st_atime, st.st_mtime))
                except:
                    pass
                copied_count += 1

        return True, f"复制完成：{copied_count} 个文件，跳过 {skipped_count} 个"
    except Exception as e:
        return False, f"复制过程出错：{str(e)}"

# ---------- 设备监控（修复版：移除多余的设备注册）----------
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
        # 不再调用 RegisterDeviceNotification，由系统自动广播 WM_DEVICECHANGE
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
        self.monitor = None
        self.tray_icon = None
        self.job_lock = threading.Lock()
        self.current_jobs = {}

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("UCopy")

        set_auto_start(self.config.get("auto_start", False))

    def on_drive_inserted(self, drive_letter):
        print(f"[检测到U盘] {drive_letter}")
        with self.job_lock:
            if drive_letter in self.current_jobs and self.current_jobs[drive_letter].is_alive():
                return
            t = threading.Thread(target=self.copy_job, args=(drive_letter,), daemon=True)
            self.current_jobs[drive_letter] = t
            t.start()

    def copy_job(self, drive_letter):
        time.sleep(1.5)  # 等待系统完全识别U盘
        success, msg = copy_drive(drive_letter, self.config)
        print(f"[{drive_letter}] {msg}")

    def start_monitor(self):
        if self.monitor is None:
            self.monitor = DeviceMonitor(self.on_drive_inserted)
            self.monitor.start()

    def stop_monitor(self):
        if self.monitor:
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
        SettingsWindow(self.root, self)

    def quit_app(self, icon=None, item=None):
        self.stop_monitor()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.quit()
        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.create_tray_icon()
        self.start_monitor()
        tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        tray_thread.start()
        self.root.after(500, self.show_settings)
        self.root.mainloop()

# ---------- 设置窗口 ----------
class SettingsWindow:
    def __init__(self, master, app):
        self.app = app
        self.win = tk.Toplevel(master)
        self.win.title("UCopy 设置")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)

        cfg = app.config

        ttk.Label(self.win, text="目标目录 (必填):").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.target_var = tk.StringVar(value=cfg["target_folder"])
        ttk.Entry(self.win, textvariable=self.target_var, width=40).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(self.win, text="浏览", command=self.browse_target).grid(row=0, column=2, padx=5)

        ttk.Label(self.win, text="最大文件数 (0=不限):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.max_files_var = tk.IntVar(value=cfg["max_total_files"])
        ttk.Entry(self.win, textvariable=self.max_files_var, width=10).grid(row=1, column=1, sticky="w", padx=5)

        ttk.Label(self.win, text="最大总大小(MB) (0=不限):").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.max_size_var = tk.IntVar(value=cfg["max_total_size_mb"])
        ttk.Entry(self.win, textvariable=self.max_size_var, width=10).grid(row=2, column=1, sticky="w", padx=5)

        ttk.Label(self.win, text="跳过单文件大于(MB) (0=不限):").grid(row=3, column=0, sticky="w", padx=5, pady=5)
        self.single_var = tk.IntVar(value=cfg["max_single_file_mb"])
        ttk.Entry(self.win, textvariable=self.single_var, width=10).grid(row=3, column=1, sticky="w", padx=5)

        ttk.Label(self.win, text="目标空间不足(MB)不复制:").grid(row=4, column=0, sticky="w", padx=5, pady=5)
        self.space_var = tk.IntVar(value=cfg["min_free_space_mb"])
        ttk.Entry(self.win, textvariable=self.space_var, width=10).grid(row=4, column=1, sticky="w", padx=5)

        ttk.Label(self.win, text="扩展名 (如 .jpg,.png):").grid(row=5, column=0, sticky="w", padx=5, pady=5)
        self.ext_var = tk.StringVar(value=cfg["extensions"])
        ttk.Entry(self.win, textvariable=self.ext_var, width=20).grid(row=5, column=1, sticky="w", padx=5)

        ttk.Label(self.win, text="文件名正则 (可空):").grid(row=6, column=0, sticky="w", padx=5, pady=5)
        self.regex_var = tk.StringVar(value=cfg["regex_pattern"])
        ttk.Entry(self.win, textvariable=self.regex_var, width=20).grid(row=6, column=1, sticky="w", padx=5)

        self.auto_var = tk.BooleanVar(value=cfg["auto_start"])
        ttk.Checkbutton(self.win, text="开机自动启动", variable=self.auto_var).grid(row=7, column=0, columnspan=2, sticky="w", padx=5, pady=5)

        btn_frame = ttk.Frame(self.win)
        btn_frame.grid(row=8, column=0, columnspan=3, pady=10)
        ttk.Button(btn_frame, text="保存并隐藏", command=self.save_and_hide).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="退出程序", command=self.app.quit_app).pack(side=tk.LEFT, padx=5)

        self.win.grab_set()

    def browse_target(self):
        folder = filedialog.askdirectory(parent=self.win)
        if folder:
            self.target_var.set(folder)

    def save_and_hide(self):
        target = self.target_var.get().strip()
        if not target:
            messagebox.showerror("错误", "目标目录不能为空", parent=self.win)
            return
        cfg = {
            "target_folder": target,
            "max_total_files": self.max_files_var.get(),
            "max_total_size_mb": self.max_size_var.get(),
            "max_single_file_mb": self.single_var.get(),
            "min_free_space_mb": self.space_var.get(),
            "extensions": self.ext_var.get().strip(),
            "regex_pattern": self.regex_var.get().strip(),
            "auto_start": self.auto_var.get()
        }
        save_config(cfg)
        self.app.config = cfg
        set_auto_start(cfg["auto_start"])
        self.win.destroy()

    def on_close(self):
        self.win.destroy()

if __name__ == "__main__":
    app = UCopyApp()
    app.run()