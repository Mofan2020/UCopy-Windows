import os
import sys
import re
import json
import time
import hashlib
import secrets
import datetime
import threading
import queue
import logging
import logging.handlers
import zlib
import struct
import base64
from copy import deepcopy
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import win32api
import win32con
import win32file
import win32gui
import win32gui_struct
import pythoncom

import device_indicator
remote_client = None
REMOTE_CLIENT_AVAILABLE = False
try:
    import remote_client as _rc
    remote_client = _rc
    REMOTE_CLIENT_AVAILABLE = True
except ImportError:
    pass

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
    # 名单模式二选一：
    #   "blacklist"  黑名单模式 —— 只有在名单里的 U 盘才跳过复制
    #   "whitelist"  白名单模式 —— 只有在名单里的 U 盘才复制
    "device_list_mode": "blacklist",
    # known_devices[uid] = {"label": str, "first_seen": str, "listed": bool}
    # "listed" 的含义随模式变化：黑名单下 true=拉黑，白名单下 true=放行
    "known_devices": {},
    # 摄像头/麦克风占用指示灯：长时间空闲时降频省 CPU（详见 device_indicator 模块）
    "indicator_slowdown": True,
    "indicator_active_interval": 1.0,
    "indicator_idle_interval": 5.0,
    # 远程模式（v1.6.0）：用 API key 连接远程服务器上传拷贝到的文件 + 同步配置
    "remote": {
        "enabled": False,
        "server_url": "",
        # v1.6.0：本机名称（显示在服务器 Web GUI 的「使用中」状态，也是独占锁的 client_id）
        "client_name": "",
        # 自动上传拷贝到本地的文件（受 extensions/regex_pattern/max_single_file_mb 过滤）
        "auto_upload": True,
        # v1.6.0：API key 的加密存储串（DPAPI / Keychain / 机器绑定 AES）。
        # 不含明文 key —— 真正的值由 ucopy_credentials 模块管理
        "api_key_enc": "",
        # 远程服务器 TLS 证书指纹（SHA-256 DER hex），首次信任后由客户端持久化
        # 这里只保存一份"上次信任的指纹"用于 UI 显示；实际信任列表在 ~/.ucopy_trusted_servers.json
        "trusted_fingerprint": "",
        # 上次拉取/推送的时间戳与错误（UI 显示用）
        "last_error": "",
        "last_upload_at": "",
        "last_config_sync_at": "",
    },
    "security": {
        "password_hash": "",
        "password_salt": "",
        "qa": []  # [{"question": str, "answer_hash": str, "answer_salt": str}, ...]
    }
}

# ---------- 名单模式（黑名单 / 白名单，二选一）----------
LIST_MODE_LABELS = {
    "blacklist": "黑名单模式",
    "whitelist": "白名单模式",
}

def normalize_list_mode(mode):
    """把任意输入收敛成合法的名单模式，非白名单一律按黑名单处理。"""
    return "whitelist" if mode == "whitelist" else "blacklist"

def list_mode_noun(mode):
    """当前模式下，把"被列入名单"称作什么（黑/白）。"""
    return "白名单" if normalize_list_mode(mode) == "whitelist" else "黑名单"

def is_device_allowed(info, mode):
    """
    判断设备在当前模式下是否允许复制。
    blacklist: 不在黑名单里 -> 允许
    whitelist: 在白名单里   -> 允许
    """
    listed = bool(info.get("listed", False))
    return listed if normalize_list_mode(mode) == "whitelist" else not listed

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
        return deepcopy(DEFAULT_CONFIG)
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
        # 名单模式归一化
        cfg["device_list_mode"] = normalize_list_mode(cfg.get("device_list_mode"))
        # 指示灯参数类型归一化（旧配置可能是错的字符串/None）
        cfg["indicator_slowdown"] = bool(cfg.get("indicator_slowdown", True))
        for k, default in (("indicator_active_interval", 1.0),
                           ("indicator_idle_interval", 5.0)):
            try:
                cfg[k] = max(0.01, float(cfg.get(k, default)))
            except (TypeError, ValueError):
                cfg[k] = default
        # 远程模式段类型归一化
        rem = cfg.setdefault("remote", {})
        if not isinstance(rem, dict):
            rem = {}
            cfg["remote"] = rem
        for k, default in (
            ("enabled", False),
            ("server_url", ""),
            ("client_name", ""),
            ("auto_upload", True),
            ("api_key_enc", ""),
            ("trusted_fingerprint", ""),
            ("last_error", ""),
            ("last_upload_at", ""),
            ("last_config_sync_at", ""),
        ):
            if k not in rem or rem[k] is None:
                rem[k] = default
        rem["enabled"] = bool(rem.get("enabled"))
        rem["auto_upload"] = bool(rem.get("auto_upload"))
        rem["server_url"] = str(rem.get("server_url") or "")
        rem["client_name"] = str(rem.get("client_name") or "")
        rem["api_key_enc"] = str(rem.get("api_key_enc") or "")
        # v1.6.0: 移除已废弃的 username 字段（v1.5.x 用）
        rem.pop("username", None)
        rem.pop("password", None)
        # 旧版本用 blacklisted 字段表示"在黑名单里"，迁移成通用的 listed。
        # 迁移必须结合当前模式：白名单模式下 listed 的含义与 blacklisted 相反。
        mode = cfg["device_list_mode"]
        for info in cfg.get("known_devices", {}).values():
            if "listed" not in info and "blacklisted" in info:
                was_blacklisted = bool(info["blacklisted"])
                info["listed"] = (not was_blacklisted) if mode == "whitelist" else was_blacklisted
        for info in cfg.get("known_devices", {}).values():
            info.pop("blacklisted", None)
        return cfg
    except Exception as e:
        # 以前这里是无声 except，读失败就回退默认值——用户配置被悄悄清空，
        # 且没有任何线索可查。至少留一条日志。
        try:
            logging.getLogger("UCopy").error(f"读取配置失败，已回退默认配置: {e}")
        except Exception:
            pass
        return deepcopy(DEFAULT_CONFIG)

def save_config(cfg):
    """
    原子写入配置文件：先写临时文件再改名，避免中途崩溃/断电留下半截 JSON。
    半截 JSON 会让 get_config 解析失败，进而把用户的密码、设备名单全部回退成默认值。
    """
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        try:
            logging.getLogger("UCopy").error(f"保存配置失败: {e}")
        except Exception:
            pass
        raise


# ---------- v1.6.0: API key 加密存储（DPAPI / Keychain / machine-bound） ----------

def _default_client_id() -> str:
    """默认客户端标识：机器名 + 短随机后缀。

    v1.7.1：固定加 6 位 hex 随机后缀，避免同 hostname 的多台机器（或同名机器
    互踢时）撞 client_id 抢占独占锁。后缀以 "-" 与主机名分隔，仍然人眼可读。
    """
    try:
        import socket as _s
        host = (_s.gethostname() or "ucopy").strip() or "ucopy"
    except Exception:
        host = "ucopy"
    # 取 hostname 前 40 字符，去掉非法字符
    safe = "".join(ch for ch in host if ch.isalnum() or ch in "-_.")
    safe = (safe or "ucopy")[:40]
    suffix = secrets.token_hex(3)  # 6 hex chars
    cid = f"{safe}-{suffix}"
    return cid[:64]


def _remote_key_account(cfg) -> str:
    """Keychain/DPAPI 的 account 名：服务器地址 + 客户端名，避免多服务器混淆。"""
    rem = (cfg or {}).get("remote", {}) or {}
    url = (rem.get("server_url") or "").strip() or "default"
    name = (rem.get("client_name") or "").strip() or "ucopy"
    return f"{url}|{name}"


def load_remote_key(cfg) -> str:
    """从加密存储读 API key；失败返回空串。"""
    try:
        rem = (cfg or {}).get("remote", {}) or {}
        blob = rem.get("api_key_enc") or ""
        if not blob:
            return ""
        import ucopy_credentials as _cred
        return _cred.load_secret(_remote_key_account(cfg), blob) or ""
    except Exception:
        return ""


def save_remote_key(cfg, api_key: str) -> bool:
    """把 API key 加密后写进 cfg["remote"]["api_key_enc"]。

    返回 True 表示已安全保存；False 表示加密后端不可用（调用方**不应**明文存）。
    注意：这里只改内存 cfg，写盘由调用方 save_config。
    """
    if not api_key:
        return False
    try:
        import ucopy_credentials as _cred
        if not _cred.is_supported():
            return False
        blob = _cred.save_secret(_remote_key_account(cfg), api_key)
        if not blob:
            return False
        cfg.setdefault("remote", {})["api_key_enc"] = blob
        return True
    except Exception:
        return False


def clear_remote_key(cfg) -> None:
    """清掉存储的 key。"""
    try:
        import ucopy_credentials as _cred
        rem = (cfg or {}).get("remote", {}) or {}
        blob = rem.get("api_key_enc") or ""
        if blob:
            _cred.delete_secret(_remote_key_account(cfg), blob)
        rem.pop("api_key_enc", None)
    except Exception:
        pass


# ---------- v1.7.1: HTTP 明文模式安全警告 ----------
# 设计：用户用 http:// 服务器时（仅内网自托管场景），首次启用/测试连接要
# 弹「明文传输」警告。含「不再提示此服务器」勾选，勾选后存到独立记忆文件。
# 记忆文件按 server_url 维度，与 TrustedServerStore 无关 —— https 模式不写入。
HTTP_WARNING_FILE = os.path.join(os.path.expanduser("~"),
                                  ".ucopy_http_acknowledged.json")


def _load_http_warning_acks() -> dict:
    """读取 HTTP 警告已确认列表（{server_url: True}）。损坏文件返回空 dict。"""
    try:
        if os.path.exists(HTTP_WARNING_FILE):
            with open(HTTP_WARNING_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_http_warning_acks(data: dict) -> None:
    """写 HTTP 警告记忆文件（原子写：tmp + os.replace）。"""
    try:
        tmp = HTTP_WARNING_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HTTP_WARNING_FILE)
    except Exception:
        # 写失败 → 静默；下次再问用户（不要 raise 让弹窗路径崩）
        pass


def has_http_warning_ack(server_url: str) -> bool:
    """该 server_url 是否已被用户确认过 HTTP 明文警告？"""
    if not server_url:
        return False
    try:
        from remote_client import normalize_server_address
        key = normalize_server_address(server_url).lower()
    except Exception:
        key = (server_url or "").strip().lower()
    return bool(_load_http_warning_acks().get(key))


def acknowledge_http_insecure(server_url: str) -> None:
    """把 server_url 写入已确认列表。"""
    if not server_url:
        return
    try:
        from remote_client import normalize_server_address
        key = normalize_server_address(server_url).lower()
    except Exception:
        key = (server_url or "").strip().lower()
    data = _load_http_warning_acks()
    data[key] = True
    _save_http_warning_acks(data)


def _http_insecure_warning(parent, server_url: str) -> bool:
    """弹「HTTP 明文传输」安全警告（v1.7.1）。

    返回 True = 用户确认继续；False = 用户取消。
    仅在用户主动进入设置面板、点「测试连接」或勾选「启用远程模式」时调用。
    已确认过的 server_url 不重复弹（has_http_warning_ack 为 True 时直接返回 True）。

    「不再提示此服务器」勾选后调 acknowledge_http_insecure(url) 持久化记忆。
    """
    if not server_url:
        return False
    if has_http_warning_ack(server_url):
        return True
    # 主动弹警告：使用 askyesno + 自定义 checkbox 不可行（Tk 原生没有
    # 「带 checkbox 的 askyesno」），所以用 Toplevel 自定义弹窗。
    win = tk.Toplevel(parent)
    win.title("HTTP 明文传输警告")
    win.transient(parent)
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: win.destroy())
    result = {"ok": False, "dont_ask": False}

    ttk.Label(win, text="服务器使用 HTTP 明文传输", font=("Helvetica", 11, "bold"),
              foreground="#b00020").grid(row=0, column=0, columnspan=2,
                                          padx=15, pady=(15, 5), sticky="w")
    ttk.Label(win,
              text=(
                  "目标服务器地址：\n"
                  f"  {server_url}\n\n"
                  "HTTP 模式下：\n"
                  "  • 数据在网络上以明文传输，可能被中间人窃听/篡改\n"
                  "  • API Key、文件内容、密码哈希都会被截获\n"
                  "  • 仅推荐在内网/完全可控的环境中使用\n\n"
                  "确定要继续吗？"),
              justify="left", wraplength=380).grid(
        row=1, column=0, columnspan=2, padx=15, pady=5, sticky="w")

    dont_ask_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(win, text="不再提示此服务器",
                    variable=dont_ask_var).grid(
        row=2, column=0, columnspan=2, padx=15, pady=(5, 10), sticky="w")

    btns = ttk.Frame(win)
    btns.grid(row=3, column=0, columnspan=2, pady=(0, 15))

    def _ok():
        result["ok"] = True
        result["dont_ask"] = bool(dont_ask_var.get())
        win.destroy()

    def _cancel():
        result["ok"] = False
        result["dont_ask"] = False
        win.destroy()

    ttk.Button(btns, text="继续（明文传输）", command=_ok).pack(
        side="left", padx=8)
    ttk.Button(btns, text="取消", command=_cancel).pack(side="left", padx=8)

    try:
        win.attributes("-topmost", True)
    except Exception:
        pass
    try:
        win.grab_set()
        parent.wait_window(win)
    except Exception:
        pass

    if result["ok"] and result["dont_ask"]:
        acknowledge_http_insecure(server_url)
    return result["ok"]


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


# ---------- 智能 diff：复制决策（mtime + size） ----------
# 用 FAT/NTFS 常见的时间戳精度 1 秒做容差：
#   - Windows FAT32/NTFS 文件系统对 mtime 的精度通常是 100ns，但很多 U 盘
#     控制器 / 跨设备拷贝会落到 1 秒甚至 2 秒；用 1 秒容差对真正"刚刚改过"
#     的文件（用户改完保存）几乎没有误判。
DEFAULT_MTIME_TOLERANCE = 1.0


def is_dest_same_as_src(src_stat, dst_stat, mtime_tolerance=DEFAULT_MTIME_TOLERANCE):
    """判断目标文件是否与源文件一致（大小 + 修改时间）。

    返回 True 表示目标已经和源相同，无需重新复制。
    """
    if src_stat is None or dst_stat is None:
        return False
    if src_stat.st_size != dst_stat.st_size:
        return False
    return abs(src_stat.st_mtime - dst_stat.st_mtime) <= mtime_tolerance


def should_skip_copy(src_file, dest_file, mtime_tolerance=DEFAULT_MTIME_TOLERANCE):
    """决定是否要跳过对 (src_file -> dest_file) 的复制。

    判定：
      - dest_file 不存在               -> 不跳过（要复制）
      - 存在 + 大小+mtime 一致（容差内） -> 跳过（视为同文件）
      - 存在 + 大小或 mtime 不同         -> 不跳过（要覆盖）

    任意一次 stat 异常（权限、临时占用、文件被删）都按「不跳过」处理，
    让复制流程自己负责报错，避免因为探测本身失败导致误跳过。
    """
    try:
        src_stat = os.stat(src_file)
    except OSError:
        return False
    try:
        dst_stat = os.stat(dest_file)
    except OSError:
        return False
    return is_dest_same_as_src(src_stat, dst_stat, mtime_tolerance)

def copy_drive(drive_letter, config, on_file_copied=None):
    """执行复制，返回 (success, message)。

    on_file_copied 可选回调：每成功复制一个文件调一次，参数 (dest_file, size, mtime)。
    用于在远程模式下入队上传（不阻塞复制主流程）。
    """
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

                # 智能 diff：目标已经和源一致（同大小 + mtime 容差内）则跳过，
                # 避免每次插入 U 盘都把整盘重写一遍。
                # 任何决策异常都按「需要复制」处理，复制流程自己负责报错。
                if should_skip_copy(src_file, dest_file):
                    skipped_count += 1
                    logger.debug(f"跳过(无变化): {src_file}")
                    continue

                try:
                    # 清掉上一次中断可能留下的 .ucopy 残留，
                    # 否则 win32file.CopyFile(False) 会直接抛「文件已存在」，
                    # 导致本次复制白白失败。
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except OSError:
                            pass
                    win32file.CopyFile(src_file, temp_file, False)
                    # 保留时间戳
                    try:
                        st = os.stat(src_file)
                        os.utime(temp_file, (st.st_atime, st.st_mtime))
                    except:
                        pass
                    # 用 os.replace 而非 os.rename：
                    # Windows 上 os.rename 遇到目标已存在会抛 FileExistsError，
                    # 导致「目标文件已存在 + 内容不同」时无法覆盖 —— 这正是用户
                    # 反馈的「复制功能工作不正常」的核心根因之一。
                    # os.replace 跨平台语义一致：原子覆盖目标。
                    os.replace(temp_file, dest_file)
                    logger.info(f"复制成功: {src_file} -> {dest_file}")
                    copied_count += 1
                    if on_file_copied is not None:
                        try:
                            st = os.stat(dest_file)
                            on_file_copied(dest_file, st.st_size, st.st_mtime)
                        except Exception as cb_err:
                            # 回调异常不能影响复制主流程
                            logger.debug(f"on_file_copied 回调异常: {cb_err}")
                except Exception as e:
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except:
                            pass
                    logger.error(f"复制失败 ({e}): {src_file}")
                    skipped_count += 1
                    continue

        msg = f"复制完成：{copied_count} 个文件被复制，跳过 {skipped_count} 个（含无变化）"
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

# ---------- 远程上传器（v1.5.0+）----------
class RemoteUploader:
    """
    后台守护线程：从 queue 拿任务，用 RemoteClient 上传文件到服务器。

    设计要点：
      - 单 daemon 线程（关窗即停）
      - 任务：{'src_file': str, 'size': int, 'mtime': float, 'rel_path': str}
      - 每次上传前调 client.check_sha256；相同跳过（节省带宽）
      - 上传失败重试 3 次（指数退避 1s / 2s / 4s）
      - 任何异常写 self.last_error + logger.warning；不弹窗（规则：自启动路径不弹）
      - v1.6.0: 用 API key 认证（不再用 username/password）
      - v1.6.0: 心跳线程每 40s 一次保独占锁；失败 5s 重试；410 = 被别人接管
    """

    def __init__(self, app):
        self.app = app
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.client = None
        self.api_key = ""          # v1.6.0 替代 username/password
        self.client_id = ""        # v1.6.0 本机标识（独占锁用）
        self.server_url = ""
        self.enabled = False  # 远程模式总开关
        self.auto_upload = True
        self.last_error = ""
        self.last_upload_at = ""
        self.uploaded_count = 0
        self.skipped_count = 0
        self.failed_count = 0
        # v1.6.0 心跳
        self._hb_thread = None
        self._hb_stop = threading.Event()
        self.key_lock_lost = False   # 被其他客户端接管
        self.key_owner_info = ""     # 当前 key 持有者（便于 UI 显示）

    def configure(self, server_url: str, api_key: str, client_id: str,
                  auto_upload: bool = True):
        """更新配置（UI 层调）。如果 server_url 变化，重建 client。"""
        new_url = (server_url or "").strip()
        if new_url != self.server_url:
            self.client = None  # 下次 _ensure_client 重建
        self.server_url = new_url
        self.api_key = api_key or ""
        self.client_id = client_id or ""
        self.auto_upload = bool(auto_upload)

    def set_enabled(self, enabled: bool):
        self.enabled = bool(enabled)
        if not enabled:
            self.last_error = ""
            self._stop_heartbeat()
            # 不清空 queue：下次启用时仍可尝试上传
        else:
            self.start()
            self._start_heartbeat()

    def start(self):
        if self.thread is None or not self.thread.is_alive():
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self, join_timeout: float = 2.0):
        self.stop_event.set()
        self._stop_heartbeat()
        if self.thread:
            self.thread.join(timeout=join_timeout)

    # ---------- v1.6.0 心跳 ----------
    def _start_heartbeat(self):
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()

    def _stop_heartbeat(self):
        self._hb_stop.set()
        if self._hb_thread:
            try:
                self._hb_thread.join(timeout=1.0)
            except Exception:
                pass
            self._hb_thread = None

    def _heartbeat_loop(self):
        """每 40s 发一次心跳；失败等 5s 重试；410 停止重试并标记 lock_lost。

        v1.7.7：client 为空时先自行登录一次。原实现只在上传任务里登录，
        开启远程模式但尚未复制文件时 client 始终为 None，心跳不会发出，
        服务器上的独占锁无人持有。
        """
        while not self._hb_stop.is_set():
            if not self.enabled:
                self._hb_stop.wait(timeout=5.0)
                continue
            client = self.client
            if client is None:
                # 还没登录 —— 先登录一次（失败就等下一轮重试）
                try:
                    self._login()
                    client = self.client
                except Exception as e:
                    self.last_error = f"心跳登录失败: {e}"
                    self._hb_stop.wait(timeout=5.0)
                    continue
            if client is None or not self.api_key:
                # 还没登录，等登录先
                self._hb_stop.wait(timeout=5.0)
                continue
            try:
                client.heartbeat()
                if self.key_lock_lost:
                    # 之前丢失过锁，现在又成功了（重新登录后）
                    self.key_lock_lost = False
                    self.last_error = ""
                self._hb_stop.wait(timeout=client.heartbeat_interval)
            except Exception as e:
                code = getattr(e, "code", "")
                if code == "key_lock_lost":
                    self.key_lock_lost = True
                    self.last_error = "该 key 已被其他设备接管（本机已断开）"
                    try:
                        self.app.logger.warning(
                            "[remote] key 被其他客户端接管，停止心跳")
                    except Exception:
                        pass
                    self._hb_stop.wait(timeout=10.0)
                else:
                    self.last_error = f"心跳失败: {e}"
                    self._hb_stop.wait(timeout=5.0)

    def enqueue(self, dest_file: str, size: int, mtime: float,
                rel_path: str):
        """UI/copy_drive 钩子入队。"""
        if not self.enabled or not self.auto_upload:
            return
        self.queue.put({
            "src_file": dest_file,
            "size": size,
            "mtime": mtime,
            "rel_path": rel_path,
        })
        self.start()  # 线程可能因异常停了，重新启

    def _ensure_client(self):
        if self.client is not None:
            return self.client
        if not self.server_url:
            raise RuntimeError("未配置服务器地址")
        if not REMOTE_CLIENT_AVAILABLE:
            raise RuntimeError("remote_client 模块不可用")
        if not remote_client:
            raise RuntimeError("remote_client 模块不可用")
        store = remote_client.TrustedServerStore()
        self.client = remote_client.RemoteClient(self.server_url, store=store)
        return self.client

    def _login(self):
        """用 API key 登录；失败抛。"""
        client = self._ensure_client()
        if not self.api_key:
            raise RuntimeError("未配置 API key")
        if not self.client_id:
            raise RuntimeError("未配置客户端标识")
        client.login_with_key(self.api_key, self.client_id)

    def _upload_one(self, task: dict):
        """单文件上传：先 check，再 upload。"""
        client = self._ensure_client()
        rel_path = task["rel_path"]
        src = task["src_file"]
        size = task["size"]
        mtime = task["mtime"]
        # 算 sha256
        sha = hashlib.sha256()
        try:
            with open(src, "rb") as f:
                while True:
                    chunk = f.read(64 * 1024)
                    if not chunk:
                        break
                    sha.update(chunk)
            sha_hex = sha.hexdigest()
        except OSError as e:
            raise RuntimeError(f"读文件失败 {src}: {e}")

        # 检查服务端是否已存在相同内容
        try:
            if client.check_sha256(rel_path, sha_hex):
                self.skipped_count += 1
                return "skipped"
        except remote_client.RemoteError as e:
            # 401 / 网络错误 → 抛上去走重试逻辑
            if e.http_status == 401:
                raise
            # 其它 4xx 视为单文件问题，继续尝试上传
            self.app.logger.debug(f"check_sha256 失败（将继续尝试上传）: {e}")

        # 上传
        client.upload_file(rel_path, src, sha_hex, size, mtime)
        self.uploaded_count += 1
        self.last_upload_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return "uploaded"

    def _run(self):
        logger = logging.getLogger("UCopy")
        backoff = [1, 2, 4]
        login_backoff_counter = 0
        while not self.stop_event.is_set():
            if not self.enabled:
                # 远程模式关闭 → 等 5s 再看
                self.stop_event.wait(timeout=5.0)
                continue
            try:
                task = self.queue.get(timeout=2.0)
            except queue.Empty:
                continue
            # 登录（如未登录）
            if self.client is None or not getattr(self.client, "_jar", None):
                try:
                    self._login()
                except Exception as e:
                    login_backoff_counter += 1
                    wait = min(backoff[min(login_backoff_counter - 1, 2)], 30)
                    self.last_error = f"登录失败: {e}"
                    logger.warning(f"[remote] 登录失败 ({login_backoff_counter}), {wait}s 后重试: {e}")
                    # 把任务放回队列头
                    self.queue.put(task)
                    self.stop_event.wait(timeout=wait)
                    continue
                login_backoff_counter = 0

            # 尝试上传（最多 3 次）
            ok = False
            for attempt in range(3):
                try:
                    result = self._upload_one(task)
                    logger.info(f"[remote] {task['rel_path']}: {result}")
                    ok = True
                    break
                except remote_client.RemoteError as e:
                    self.last_error = f"上传失败 ({e.code}): {e.message}"
                    logger.warning(f"[remote] 上传失败 attempt={attempt+1}: {e}")
                    if e.http_status == 401:
                        # session 过期 → 清 client，下次重新登录
                        self.client = None
                        break
                    if attempt < 2:
                        self.stop_event.wait(timeout=backoff[attempt])
                except Exception as e:
                    self.last_error = f"上传异常: {e}"
                    logger.exception(f"[remote] 上传异常 attempt={attempt+1}: {e}")
                    if attempt < 2:
                        self.stop_event.wait(timeout=backoff[attempt])
            if not ok:
                self.failed_count += 1


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

# ---------- 桌面悬浮小圆点 ----------
def _hex_to_rgb(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _blend(c1, c2, ratio):
    """按 ratio(0~1) 把 c1 混向 c2"""
    return tuple(int(round(a + (b - a) * ratio)) for a, b in zip(c1, c2))


def build_dot_png(win_px, diameter, color, key, supersample=16):
    """
    生成一张 win_px x win_px 的 PNG：中央为 diameter 像素的圆，
    圆边缘做 supersample 抗锯齿处理，并把半透明像素预先混向透明键色。

    这样即使 Tk 不支持带 alpha 的图片，边缘也不会出现锯齿或黑边。
    """
    fg = _hex_to_rgb(color)
    bg = _hex_to_rgb(key)
    r = diameter / 2.0
    cx = cy = win_px / 2.0
    ss = max(1, int(supersample))
    rows = []
    for y in range(win_px):
        row = bytearray()
        for x in range(win_px):
            inside = 0
            for sy in range(ss):
                py = y + (sy + 0.5) / ss
                for sx in range(ss):
                    px = x + (sx + 0.5) / ss
                    if (px - cx) ** 2 + (py - cy) ** 2 <= r * r:
                        inside += 1
            cover = inside / float(ss * ss)
            if cover <= 0:
                rgb = bg
            elif cover >= 1:
                rgb = fg
            else:
                rgb = _blend(bg, fg, cover)
            row += bytes(rgb)
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + row for row in rows)  # 每行前缀 0 = 无 PNG filter

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", win_px, win_px, 8, 2, 0, 0, 0)  # 8bit truecolor RGB
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


class FloatingDot:
    """
    屏幕右上角常驻的紫色小圆点，替代原来的系统托盘图标。
    无边框、置顶、背景色抠除做透明，连续点击 3 次才打开设置面板。
    """
    SIZE = 6           # 小圆点直径（像素）
    HIT = 24           # 透明点击区域边长，让小点也好连续点中
    MARGIN = 6         # 距屏幕右边缘 / 上边缘的像素
    CLICKS_TO_OPEN = 3 # 需要连续点击的次数
    CLICK_INTERVAL = 1.2   # 相邻两次点击的最大间隔（秒），超时则重新计数
    DOT_COLOR = "#8B5CF6"
    TRANSPARENT_KEY = "#0A0B0C"   # 魔术色，与紫色差异大，用作透明键

    def __init__(self, master, on_activate):
        self.on_activate = on_activate
        self._clicks = 0
        self._last_click = 0.0
        self.win = tk.Toplevel(master)
        self.win.overrideredirect(True)
        self.win.title("UCopy")
        try:
            self.win.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            # Windows 下用指定色做完全透明
            self.win.attributes("-transparentcolor", self.TRANSPARENT_KEY)
        except tk.TclError:
            pass

        self.current_color = self.DOT_COLOR
        self._make_photo(self.DOT_COLOR)
        self.label = tk.Label(
            self.win, image=self.photo, bg=self.TRANSPARENT_KEY,
            highlightthickness=0, bd=0, padx=0, pady=0
        )
        self.label.pack()
        # 只在顶层窗口上绑定单击：若 label 和 toplevel 都绑，
        # 一次点击会因 Tk 的 bindtag 传播被派发两次，导致计数翻倍
        self.win.bind("<Button-1>", self._on_click)
        self.win.bind("<Button-2>", self._on_click)
        self.win.bind("<Button-3>", self._on_click)

        self.place_top_right()

    def _make_photo(self, color):
        """按给定颜色重新生成圆点图片（抗锯齿参数保持不变）"""
        png = build_dot_png(self.HIT, self.SIZE, color, self.TRANSPARENT_KEY)
        self.photo = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))

    def set_color(self, color):
        """
        更新圆点颜色（摄像头在用=绿、仅麦克风在用=橙、空闲=紫）。
        颜色没变则不重建图片，避免每秒轮询都刷一次控件。
        """
        if not color or color == self.current_color:
            return
        self.current_color = color
        try:
            self._make_photo(color)
            self.label.configure(image=self.photo)
        except tk.TclError:
            # 窗口已销毁（程序退出中），忽略
            pass

    def _on_click(self, event=None):
        """连续点击 CLICKS_TO_OPEN 次才触发，超时未达次数则重新计数"""
        now = time.time()
        if now - self._last_click > self.CLICK_INTERVAL:
            self._clicks = 0
        self._last_click = now
        self._clicks += 1
        if self._clicks < self.CLICKS_TO_OPEN:
            # 中途超时自动归零
            self.win.after(int(self.CLICK_INTERVAL * 1000), self._reset_clicks_if_idle)
            # 返回 "break" 阻止事件继续沿 bindtag 传播，杜绝重复计数
            return "break"
        self._clicks = 0
        # 返回 "break" 阻止事件继续沿 bindtag 传播，杜绝重复触发
        self.on_activate()
        return "break"

    def _reset_clicks_if_idle(self):
        now = time.time()
        if self._clicks and now - self._last_click >= self.CLICK_INTERVAL:
            self._clicks = 0

    def _activate(self, event=None):
        # 返回 "break" 阻止事件继续沿 bindtag 传播，杜绝重复触发
        self.on_activate()
        return "break"

    def place_top_right(self):
        self.win.update_idletasks()
        sw = self.win.winfo_screenwidth()
        x = max(0, sw - self.HIT - self.MARGIN)
        self.win.geometry(f"{self.HIT}x{self.HIT}+{x}+{self.MARGIN}")

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass

# ---------- 主应用 ----------
class UCopyApp:
    def __init__(self):
        self.config = get_config()
        self.logger = None
        self.monitor = None
        self.floating_dot = None
        self.indicator = None      # 摄像头/麦克风占用检测器
        self.panel_open = False   # 设置面板/登录窗是否已打开（防重复创建）
        self.panel_win = None
        self._last_open_ts = 0.0  # 打开设置面板的防抖时间戳
        self.job_lock = threading.Lock()
        self.current_jobs = {}
        # 远程模式（v1.5.0+）：后台线程从队列拿任务上传
        self.remote_uploader = RemoteUploader(self)
        self._init_remote_from_config()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("UCopy")

        set_auto_start(self.config.get("auto_start", False))

    def _init_remote_from_config(self):
        """把 config.remote 同步到 RemoteUploader（v1.6.0：从加密存储读 key）。"""
        rem = self.config.get("remote", {}) or {}
        api_key = load_remote_key(self.config) or ""
        client_id = rem.get("client_name") or _default_client_id()
        self.remote_uploader.configure(
            server_url=rem.get("server_url", ""),
            api_key=api_key,
            client_id=client_id,
            auto_upload=rem.get("auto_upload", True),
        )
        self.remote_uploader.set_enabled(bool(rem.get("enabled", False)))

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
        mode = normalize_list_mode(self.config.get("device_list_mode"))
        known = self.config.setdefault("known_devices", {})
        if unique_id not in known:
            known[unique_id] = {
                "label": label,
                "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                # 白名单模式下新设备默认不在名单里（不会被复制）；
                # 黑名单模式下默认不在名单里（会被复制），保持历史行为。
                "listed": False
            }
            self.logger.info(f"新设备已记录: {label} ({unique_id})")
            save_config(self.config)
        else:
            # 更新标签（可能发生了变化）
            known[unique_id]["label"] = label
            save_config(self.config)

        # 按当前名单模式决定是否复制
        if not is_device_allowed(known[unique_id], mode):
            verb = "不在白名单中，不复制" if mode == "whitelist" else "在黑名单中，跳过复制"
            self.logger.info(f"U盘 {label} {verb}")
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
        logger = self.logger or logging.getLogger("UCopy")

        def _on_copied(dest_file, size, mtime):
            # dest_file 是本地的完整路径；rel_path 用相对 target_folder
            try:
                target_base = self.config.get("target_folder", "")
                if target_base and dest_file.lower().startswith(
                        target_base.lower()):
                    rel = os.path.relpath(dest_file, target_base)
                else:
                    rel = os.path.basename(dest_file)
                # Windows 路径分隔符统一为 /，与服务端约定
                rel = rel.replace(os.sep, "/")
                self.remote_uploader.enqueue(dest_file, size, mtime, rel)
            except Exception as e:
                logger.debug(f"远程入队失败: {e}")

        success, msg = copy_drive(drive_letter, self.config,
                                  on_file_copied=_on_copied)
        logger.info(f"复制任务结束 ({drive_letter}): {msg}")

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

    def create_floating_dot(self):
        """在屏幕右上角放一个置顶的紫色小圆点，连续点击三次打开设置面板。"""
        self.floating_dot = FloatingDot(self.root, on_activate=self.show_settings)

    def start_indicator(self):
        """
        启动摄像头/麦克风占用检测：
          - 有程序在用摄像头 -> 小圆点变绿（麦克风同时在用也仍然是绿）
          - 摄像头没人用、只有麦克风在用 -> 变橙
          - 都空闲 -> 恢复紫色
        轮询在后台线程里跑，控件更新统一切回主线程（Tk 非线程安全）。

        长时间空闲时会自动降频（默认 5s 一次），检测到占用立刻恢复到 1s，
        保证首次变化延迟仍是 1s。可通过 config["indicator_slowdown"]=False 关掉。
        """
        try:
            cfg = self.config
            self.indicator = device_indicator.IndicatorMonitor(
                active_interval=float(cfg.get("indicator_active_interval", 1.0)),
                idle_interval=float(cfg.get("indicator_idle_interval", 5.0)),
                enable_idle_slowdown=bool(cfg.get("indicator_slowdown", True)),
                on_change=self._on_indicator_change,
                on_unknown_streak=self._on_indicator_unknown_streak,
            )
            self.indicator.start()
            self.logger.info("摄像头/麦克风占用检测已启动（虚拟设备同样覆盖）")
        except Exception as e:
            self.logger.error(f"摄像头/麦克风检测启动失败，小圆点保持紫色: {e}")
            self.indicator = None

    def _on_indicator_change(self, state):
        """检测线程回调：后台线程不能直接改 Tk，必须切回主线程"""
        try:
            color = device_indicator.state_color(state)
            self.root.after(0, lambda: self._apply_indicator_color(color, state))
        except Exception:
            pass

    def _apply_indicator_color(self, color, state):
        if not self.floating_dot:
            return
        self.floating_dot.set_color(color)
        if state != device_indicator.STATE_IDLE:
            self.logger.info(f"小圆点状态切换: {state} ({color})")

    def _on_indicator_unknown_streak(self, n):
        """持续 N 次探测都失败（连续 (None, None)）时记录一条 warning。

        n=0 是恢复信号（已探测到真实状态），不记录。n≥1 时写日志一次即可，
        设备指示灯模块内部已防 spam。
        """
        if n <= 0:
            return
        try:
            self.logger.warning(
                f"摄像头/麦克风检测已连续 {n} 次失败（无法判定占用状态，"
                f"指示灯保持紫色）"
            )
        except Exception:
            pass

    def stop_indicator(self):
        if self.indicator:
            try:
                self.indicator.stop()
                self.logger.info("摄像头/麦克风占用检测已停止")
            except Exception:
                pass
            self.indicator = None

    def show_settings(self, event=None):
        """
        打开设置面板的统一入口（小圆点连续点击三次触发）：
          - 若已有面板/登录窗在开，直接前置，不重复创建
          - 若未设置过密码，强制进入 SecuritySetupWindow
          - 若已设置，弹 LoginWindow 校验密码
          - 校验通过后回调进入 SettingsWindow

        注意：面板占用标记从“弹登录窗”这一刻就置位（不是等到 SettingsWindow），
        否则登录窗阶段 panel_open 仍为 False，一次点击会开出两个密码窗。
        """
        now = time.time()
        # 事件可能因 Tk 的 bindtag 传播被派发多次，这里做 1 秒防抖兜底
        if now - self._last_open_ts < 1.0:
            return
        self._last_open_ts = now

        if self.panel_open:
            existing = self.panel_win
            try:
                if existing is not None and existing.winfo_exists():
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass
            self.panel_open = False

        def _open_settings():
            self.panel_open = True
            SettingsWindow(self.root, self, on_close=self._close_panel)

        # 立刻占位：登录窗 / 首次设置窗期间也禁止重复创建
        self.panel_open = True
        sec = self.config.get("security", {})
        if not sec.get("password_hash"):
            SecuritySetupWindow(self.root, self, on_success=_open_settings,
                                on_close=self._close_panel)
        else:
            LoginWindow(self.root, self, on_success=_open_settings,
                        on_close=self._close_panel)

    def _close_panel(self):
        self.panel_open = False
        self.panel_win = None

    def quit_app(self, icon=None, item=None):
        self.logger.info("用户请求退出程序")
        try:
            self.remote_uploader.stop()
        except Exception:
            pass
        self.stop_monitor()
        self.stop_indicator()
        if self.floating_dot:
            self.floating_dot.destroy()
        self.root.quit()
        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.init_logger()
        self.create_floating_dot()
        self.start_monitor()
        self.start_indicator()
        # 首次启动引导设置密码；已有密码则不打扰，点击三次小圆点即可打开面板
        if not self.config.get("security", {}).get("password_hash"):
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

    def __init__(self, master, app, on_success, on_close=None):
        self.app = app
        self.on_success = on_success  # 回调：完成设置后进入 SettingsWindow
        self.on_close = on_close      # 回调：窗口关闭时释放面板占用标记
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
        if self.on_close:
            self.on_close()
        if self.on_success:
            self.on_success()


# ---------- 登录窗口（每次进入设置前） ----------
class LoginWindow:
    """
    校验登录密码。校验通过后回调 on_success 进入 SettingsWindow。
    忘记密码入口：跳到 ForgotPasswordWindow，走密保问题校验后重置密码。
    """
    MAX_ATTEMPTS = 5  # 失败超过次数强制退出

    def __init__(self, master, app, on_success, on_close=None):
        self.app = app
        self.on_success = on_success
        self.on_close = on_close      # 回调：窗口关闭时释放面板占用标记
        self.attempts = 0
        self._locked = False  # 触顶后置 True，幂等保护
        self.win = tk.Toplevel(master)
        self.win.title("UCopy - 验证密码")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)

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
        ttk.Button(btn_frame, text="取消", command=self.cancel).pack(side=tk.LEFT, padx=5)

        self.win.bind("<Return>", lambda e: self.verify())
        self.win.grab_set()

    def _close(self):
        """统一的窗口关闭：释放 grab、销毁窗口、释放面板占用标记"""
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close()

    def cancel(self):
        """
        取消登录：只关闭密码窗口，程序与后台 U 盘复制继续运行。
        （旧实现会直接退出整个程序，导致误点取消后 UCopy 静默消失）
        """
        try:
            self.app.logger.info("用户取消密码输入，关闭登录窗口")
        except Exception:
            pass
        self._close()

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
            self._close()
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
            # 留下审计日志供事后追溯。需要再试可再次点击三次右上角小圆点。
            try:
                self.app.logger.warning(
                    f"设置面板密码连续 {self.MAX_ATTEMPTS} 次错误，临时锁定，"
                    "U 盘复制功能未受影响"
                )
            except Exception:
                pass
            self._locked = True
            self._close()
            messagebox.showerror(
                "已临时锁定",
                f"密码连续 {self.MAX_ATTEMPTS} 次错误，已关闭设置面板。\n"
                "后台 U 盘复制功能继续运行，需要时可再次点击三次右上角小圆点。",
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
        self.win.grab_release()
        self.win.withdraw()
        ForgotPasswordWindow(self.win, self.app, qa, on_reset=self._on_reset_done)

    def _on_reset_done(self):
        """密保校验通过、重置密码后：关掉（已隐藏的）登录窗并直接进入设置"""
        self._close()
        if self.on_success:
            self.on_success()


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


# ---------- 远程模式：强制确认弹窗 ----------
class RemoteConsentWindow:
    """
    开启远程模式前的强制确认弹窗。三项 checkbox 必须全部勾选才能启用。

    设计：作为 modal 子窗口（grab_set + wait_window），返回 True/False。
    """

    CONSENT_ITEMS = [
        "我仅将本工具用于合法用途，因滥用产生的全部责任由我本人承担。",
        "我没有受到欺骗或诱导而启用此模式。",
        "我已确认服务器地址正确，且服务器有足够空间存储上传的文件。",
    ]

    def __init__(self, master, server_url: str):
        self.win = tk.Toplevel(master)
        self.win.title("启用远程模式 — 重要确认")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.result = False

        frm = ttk.Frame(self.win, padding=14)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="⚠️  启用远程模式前请逐条确认以下事项：",
                  font=("", 11, "bold")).grid(row=0, column=0,
                                             columnspan=2, sticky="w",
                                             pady=(0, 8))

        info = ttk.Label(frm,
                         text=f"服务器地址：\n{server_url or '(未填写)'}",
                         foreground="#444", wraplength=420, justify="left")
        info.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self.checks = []
        for i, text in enumerate(self.CONSENT_ITEMS):
            var = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(frm, text=text, variable=var,
                                 command=self._refresh_state)
            cb.grid(row=2 + i, column=0, columnspan=2, sticky="w",
                    padx=4, pady=2)
            self.checks.append(var)

        # 按钮区
        btn = ttk.Frame(frm)
        btn.grid(row=2 + len(self.CONSENT_ITEMS), column=0,
                 columnspan=2, pady=(16, 0))
        self.ok_btn = ttk.Button(btn, text="启用（必须三项都勾选）",
                                 command=self._on_ok, state="disabled")
        self.ok_btn.pack(side="left", padx=6)
        ttk.Button(btn, text="取消", command=self._on_cancel).pack(
            side="left", padx=6)

        self.win.grab_set()
        self._refresh_state()

    def _all_checked(self) -> bool:
        return all(v.get() for v in self.checks)

    def _refresh_state(self):
        self.ok_btn.config(state=("normal" if self._all_checked()
                                  else "disabled"))

    def _on_ok(self):
        if not self._all_checked():
            return
        self.result = True
        self.win.destroy()

    def _on_cancel(self):
        self.result = False
        self.win.destroy()

    def show(self) -> bool:
        self.win.wait_window()
        return self.result


# ---------- 设置窗口 ----------
class SettingsWindow:
    # v1.7.1：自适应布局（外层 Canvas + 内部 frame + 垂直 scrollbar；窗口可调尺寸）
    MIN_WIDTH = 480
    MIN_HEIGHT = 420

    def __init__(self, master, app, on_close=None):
        self.app = app
        self.on_close_cb = on_close
        self.win = tk.Toplevel(master)
        self.win.title("UCopy 设置")
        # v1.7.1：允许拖拽改变尺寸（高 DPI / 小屏用户能调出完整 UI）
        self.win.resizable(True, True)
        self.win.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.win.protocol("WM_DELETE_WINDOW", self.on_close)
        app.panel_win = self.win

        cfg = app.config

        # v1.7.1：外层结构 —— Canvas + vertical scrollbar + 内嵌 Frame，
        # 内容超出窗口高度时滚动显示（避免小屏上底部按钮看不到）。
        # Canvas 自身用 grid 占满窗口，columnconfigure 让 scrollbar 跟随拉伸。
        self._outer_canvas = tk.Canvas(self.win, highlightthickness=0, bd=0)
        self._outer_scroll = ttk.Scrollbar(
            self.win, orient="vertical", command=self._outer_canvas.yview)
        self._outer_canvas.configure(yscrollcommand=self._outer_scroll.set)
        self._outer_canvas.grid(row=0, column=0, sticky="nsew")
        self._outer_scroll.grid(row=0, column=1, sticky="ns")
        # 让 canvas 跟随窗口大小变化
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(0, weight=1)

        self._scroll_frame = ttk.Frame(self._outer_canvas)
        # window id 留着用于重新计算 scrollregion
        self._scroll_window = self._outer_canvas.create_window(
            (0, 0), window=self._scroll_frame, anchor="nw")

        # 内容超长时让 scrollregion 自动撑大
        def _on_inner_config(event):
            try:
                self._outer_canvas.configure(
                    scrollregion=self._outer_canvas.bbox("all"))
            except Exception:
                pass
        self._scroll_frame.bind("<Configure>", _on_inner_config, add="+")

        # canvas 大小变化时让内部 frame 同步宽
        def _on_canvas_config(event):
            try:
                self._outer_canvas.itemconfigure(
                    self._scroll_window, width=event.width)
            except Exception:
                pass
        self._outer_canvas.bind("<Configure>", _on_canvas_config, add="+")

        # 鼠标滚轮（仅当 canvas 有焦点时）
        def _on_wheel(event):
            try:
                if self._outer_canvas.winfo_height() < (
                        self._scroll_frame.winfo_height()):
                    self._outer_canvas.yview_scroll(
                        int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass
        self._outer_canvas.bind("<Enter>",
                                lambda e: self._outer_canvas.bind_all(
                                    "<MouseWheel>", _on_wheel))
        self._outer_canvas.bind("<Leave>",
                                lambda e: self._outer_canvas.unbind_all(
                                    "<MouseWheel>"))

        # 真正承载子模块的 parent（从此处开始所有 grid 都进 _scroll_frame）
        parent = self._scroll_frame
        # 列权重：column 1（输入框）跟随窗口拉伸
        parent.columnconfigure(1, weight=1)

        # 基本设置
        frame_basic = ttk.LabelFrame(parent, text="基本设置", padding=5)
        frame_basic.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="ew")
        frame_basic.columnconfigure(1, weight=1)

        ttk.Label(frame_basic, text="目标目录 (必填):").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.target_var = tk.StringVar(value=cfg["target_folder"])
        ttk.Entry(frame_basic, textvariable=self.target_var).grid(
            row=0, column=1, padx=5, pady=2, sticky="ew")
        ttk.Button(frame_basic, text="浏览", command=self.browse_target).grid(
            row=0, column=2, padx=5)

        ttk.Label(frame_basic, text="最大文件数 (0=不限):").grid(
            row=1, column=0, sticky="w", padx=5, pady=2)
        self.max_files_var = tk.IntVar(value=cfg["max_total_files"])
        ttk.Entry(frame_basic, textvariable=self.max_files_var, width=10).grid(
            row=1, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="最大总大小(MB) (0=不限):").grid(
            row=2, column=0, sticky="w", padx=5, pady=2)
        self.max_size_var = tk.IntVar(value=cfg["max_total_size_mb"])
        ttk.Entry(frame_basic, textvariable=self.max_size_var, width=10).grid(
            row=2, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="跳过单文件大于(MB) (0=不限):").grid(
            row=3, column=0, sticky="w", padx=5, pady=2)
        self.single_var = tk.IntVar(value=cfg["max_single_file_mb"])
        ttk.Entry(frame_basic, textvariable=self.single_var, width=10).grid(
            row=3, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="目标空间不足(MB)不复制:").grid(
            row=4, column=0, sticky="w", padx=5, pady=2)
        self.space_var = tk.IntVar(value=cfg["min_free_space_mb"])
        ttk.Entry(frame_basic, textvariable=self.space_var, width=10).grid(
            row=4, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="扩展名 (如 .jpg,.png):").grid(
            row=5, column=0, sticky="w", padx=5, pady=2)
        self.ext_var = tk.StringVar(value=cfg["extensions"])
        ttk.Entry(frame_basic, textvariable=self.ext_var, width=20).grid(
            row=5, column=1, sticky="w", padx=5)

        ttk.Label(frame_basic, text="文件名正则 (可空):").grid(
            row=6, column=0, sticky="w", padx=5, pady=2)
        self.regex_var = tk.StringVar(value=cfg["regex_pattern"])
        ttk.Entry(frame_basic, textvariable=self.regex_var, width=20).grid(
            row=6, column=1, sticky="w", padx=5)

        self.auto_var = tk.BooleanVar(value=cfg["auto_start"])
        ttk.Checkbutton(frame_basic, text="开机自动启动",
                        variable=self.auto_var).grid(
            row=7, column=0, columnspan=2, sticky="w", padx=5, pady=5)

        # ---- 名单模式（黑名单 / 白名单，二选一）----
        frame_list = ttk.LabelFrame(parent, text="U盘名单", padding=5)
        frame_list.grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")
        frame_list.columnconfigure(0, weight=1)
        frame_list.rowconfigure(1, weight=1)

        self.list_mode_var = tk.StringVar(
            value=normalize_list_mode(cfg.get("device_list_mode"))
        )
        ttk.Label(frame_list, text="工作模式:").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.mode_combo = ttk.Combobox(
            frame_list, textvariable=self.list_mode_var,
            values=["blacklist", "whitelist"], state="readonly", width=12
        )
        self.mode_combo.grid(row=0, column=1, sticky="w", padx=5, pady=2)
        self.mode_combo.bind("<<ComboboxSelected>>", self.on_mode_changed)
        self.mode_hint_var = tk.StringVar()
        ttk.Label(frame_list, textvariable=self.mode_hint_var,
                  foreground="gray").grid(
            row=0, column=2, sticky="w", padx=8)

        # Listbox 拉伸：跟随窗口宽度
        self.device_listbox = tk.Listbox(
            frame_list, height=6, selectmode=tk.MULTIPLE,
            highlightthickness=1)
        self.device_listbox.grid(row=1, column=0, columnspan=3, padx=5, pady=5,
                                 sticky="nsew")
        scrollbar = ttk.Scrollbar(frame_list, orient=tk.VERTICAL,
                                  command=self.device_listbox.yview)
        scrollbar.grid(row=1, column=3, sticky="ns", padx=(0, 5))
        self.device_listbox.config(yscrollcommand=scrollbar.set)

        btn_frame_black = ttk.Frame(frame_list)
        btn_frame_black.grid(row=2, column=0, columnspan=4, pady=(2, 0))
        self.toggle_btn = ttk.Button(btn_frame_black, command=self.toggle_listed)
        self.toggle_btn.pack(side=tk.LEFT, padx=5)

        self.on_mode_changed()   # 初始化按钮文字与提示

        # ---- 远程模式 ----
        frame_remote = ttk.LabelFrame(parent, text="远程模式（v1.5.0+）",
                                       padding=5)
        frame_remote.grid(row=2, column=0, columnspan=2, padx=5, pady=5,
                          sticky="nsew")
        frame_remote.columnconfigure(1, weight=1)
        self._build_remote_frame(frame_remote)

        # 底部按钮
        btn_frame = ttk.Frame(parent)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="保存并隐藏", command=self.save_and_hide).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="修改密码", command=self.change_password).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="修改密保", command=self.change_qa).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="退出程序", command=self.app.quit_app).pack(side=tk.LEFT, padx=5)

        self.win.grab_set()

    def _build_remote_frame(self, parent):
        """构造远程模式子面板（v1.6.0：API key 认证 + v1.7.1：自适应）。"""
        cfg_remote = (self.app.config.get("remote") or {})
        # v1.7.1：列权重已经在 SettingsWindow 设过（column 1 weight=1），
        # 这里给 frame 列再设一次，让远程面板内部也能横向拉伸。
        try:
            parent.columnconfigure(1, weight=1)
            parent.columnconfigure(2, weight=1)
        except Exception:
            pass
        # 服务器地址
        ttk.Label(parent, text="服务器地址 (https://host:8443):").grid(
            row=0, column=0, sticky="w", padx=5, pady=2)
        self.remote_url_var = tk.StringVar(value=cfg_remote.get("server_url", ""))
        ttk.Entry(parent, textvariable=self.remote_url_var).grid(
            row=0, column=1, columnspan=2, padx=5, pady=2, sticky="ew")

        # API Key（v1.6.0：替代用户名/密码）
        ttk.Label(parent, text="API Key:").grid(row=1, column=0, sticky="w",
                                               padx=5, pady=2)
        self.remote_key_var = tk.StringVar()
        self.remote_key_entry = ttk.Entry(parent, textvariable=self.remote_key_var,
                                         show="*")
        self.remote_key_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=2,
                                   sticky="ew")
        saved_key_state = "已保存" if cfg_remote.get("api_key_enc") else "未设置"
        ttk.Label(parent, text=f"（{saved_key_state}；留空则用已保存的 key）",
                  foreground="#666", font=("Helvetica", 9)).grid(
            row=2, column=1, columnspan=2, sticky="w", padx=5)

        # 客户端名称（显示在服务器 Web GUI 的"使用中"状态里）
        ttk.Label(parent, text="本机名称:").grid(row=3, column=0, sticky="w",
                                               padx=5, pady=2)
        # v1.7.1：默认值带 6 位随机后缀，避免同 hostname 的多台机器互踢独占锁
        default_client_name = (cfg_remote.get("client_name")
                               or _default_client_id())
        self.remote_client_name_var = tk.StringVar(value=default_client_name)
        ttk.Entry(parent, textvariable=self.remote_client_name_var, width=30).grid(
            row=3, column=1, padx=5, pady=2, sticky="ew")
        ttk.Label(parent, text="（便于在服务器上区分设备）",
                  foreground="#666", font=("Helvetica", 9)).grid(
            row=3, column=2, sticky="w")

        # 启用 + 自动上传
        self.remote_enabled_var = tk.BooleanVar(value=bool(cfg_remote.get("enabled", False)))
        self.remote_auto_var = tk.BooleanVar(value=bool(cfg_remote.get("auto_upload", True)))
        # v1.7.7：这两个 checkbox 必须有 command 回调。只绑 variable 的话，
        # 勾选动作不执行任何代码，_persist_remote 无人调用，
        # RemoteUploader.enabled 恒为 False，enqueue 首行直接 return，
        # 文件不会上传。
        ttk.Checkbutton(parent, text="启用远程模式",
                        variable=self.remote_enabled_var,
                        command=self._on_remote_enable_toggle).grid(
            row=4, column=0, sticky="w", padx=5, pady=2)
        ttk.Checkbutton(parent, text="自动上传已拷贝文件",
                        variable=self.remote_auto_var,
                        command=self._on_remote_auto_toggle).grid(
            row=4, column=1, sticky="w", padx=5, pady=2)

        # 指纹 + 状态
        self.remote_fp_var = tk.StringVar(value=cfg_remote.get("trusted_fingerprint", ""))
        ttk.Label(parent, text="服务器证书指纹:").grid(
            row=5, column=0, sticky="w", padx=5, pady=2)
        ttk.Label(parent, textvariable=self.remote_fp_var, foreground="#555",
                  font=("Courier", 9)).grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=5, pady=2)

        self.remote_status_var = tk.StringVar(value=self._status_text())
        # v1.7.1：wraplength 不再写死，跟随窗口宽度；用 width 字段失效就
        # 用 winfo_width 跟随。
        self.remote_status_label = ttk.Label(parent, textvariable=self.remote_status_var,
                                             foreground="#333",
                                             justify="left", anchor="w")
        self.remote_status_label.grid(
            row=6, column=0, columnspan=3, sticky="ew", padx=5, pady=2)
        # 跟随 canvas/frame 宽度调整 wraplength
        def _adjust_status_wrap(event=None):
            try:
                w = parent.winfo_width()
                if w > 50:
                    self.remote_status_label.configure(wraplength=max(200, w - 40))
            except Exception:
                pass
        parent.bind("<Configure>", _adjust_status_wrap, add="+")

        # 操作按钮
        btn_row = ttk.Frame(parent)
        btn_row.grid(row=7, column=0, columnspan=3, pady=(6, 0))
        ttk.Button(btn_row, text="测试连接", command=self.test_remote).pack(
            side="left", padx=4)
        ttk.Button(btn_row, text="从服务器拉取配置",
                   command=self.pull_remote_config).pack(side="left", padx=4)
        ttk.Button(btn_row, text="推送配置到服务器",
                   command=self.push_remote_config).pack(side="left", padx=4)

    def _get_remote_key(self):
        """取 API key：输入框优先，否则从加密存储读。"""
        typed = self.remote_key_var.get().strip()
        if typed:
            return typed, True  # (key, needs_save)
        stored = load_remote_key(self.app.config)
        if stored:
            return stored, False
        return "", False

    def _status_text(self):
        u = self.app.remote_uploader
        parts = []
        if u.last_upload_at:
            parts.append(f"上次上传: {u.last_upload_at}")
        if u.last_error:
            parts.append(f"错误: {u.last_error}")
        parts.append(f"已上传 {u.uploaded_count} / 跳过 {u.skipped_count} / 失败 {u.failed_count}")
        return " | ".join(parts)

    def _refresh_remote_status(self):
        self.remote_status_var.set(self._status_text())

    def _on_remote_enable_toggle(self):
        """「启用远程模式」checkbox 的 command 回调（v1.7.7）。

        只绑 variable 的话勾选不执行代码，_persist_remote 无人调用，
        RemoteUploader.enabled 恒为 False，文件不会上传。

        勾选/取消都要落盘并同步到 uploader。
        """
        try:
            self._persist_remote(self.remote_enabled_var.get())
        except Exception as e:
            try:
                self.app.logger.error(f"[remote] 启用状态切换失败: {e}")
            except Exception:
                pass

    def _on_remote_auto_toggle(self):
        """「自动上传已拷贝文件」checkbox 的 command 回调（v1.7.7）。"""
        auto = bool(self.remote_auto_var.get())
        rem = self.app.config.setdefault("remote", {})
        rem["auto_upload"] = auto
        try:
            save_config(self.app.config)
        except Exception:
            pass
        self.app.remote_uploader.auto_upload = auto
        try:
            self.app.logger.info(f"[remote] 自动上传已{'开启' if auto else '关闭'}")
        except Exception:
            pass
        self._refresh_remote_status()

    def _persist_remote(self, enabled_now: bool):
        """把 UI 的 remote 段写回 cfg + 同步到 RemoteUploader，启用时弹强制确认。

        v1.7.1：启用路径在 RemoteConsentWindow 之前先检查 HTTP 明文警告，
        用户取消 → 整个启用流程取消（与「未勾合法用途三项」同一处理）。
        """
        new_enabled = bool(self.remote_enabled_var.get())
        # 关闭路径：不弹确认，直接关
        if not new_enabled:
            self.app.config.setdefault("remote", {})
            self.app.config["remote"].update({
                "enabled": False,
                "server_url": self.remote_url_var.get().strip(),
                "client_name": self.remote_client_name_var.get().strip(),
                "auto_upload": bool(self.remote_auto_var.get()),
            })
            try:
                save_config(self.app.config)
            except Exception:
                pass
            rem = self.app.config["remote"]
            self.app.remote_uploader.configure(
                server_url=rem["server_url"],
                api_key=load_remote_key(self.app.config),
                client_id=rem.get("client_name") or _default_client_id(),
                auto_upload=rem["auto_upload"],
            )
            self.app.remote_uploader.set_enabled(False)
            try:
                self.app.logger.info("[remote] 远程模式已关闭")
            except Exception:
                pass
            return True

        # 启用路径（v1.6.0：API key）
        url = self.remote_url_var.get().strip()
        client_name = self.remote_client_name_var.get().strip()
        api_key, needs_save = self._get_remote_key()
        if not url:
            messagebox.showerror("错误", "请填写服务器地址", parent=self.win)
            return False
        if not client_name:
            messagebox.showerror("错误", "请填写本机名称", parent=self.win)
            return False
        if not api_key:
            messagebox.showerror(
                "错误",
                "请填写 API Key。\n\n"
                "在服务器 Web GUI 登录后 → 「API Keys」卡片 → 创建，\n"
                "复制生成的 key 粘贴到这里。",
                parent=self.win)
            return False
        # v1.7.1：HTTP 明文模式警告（在 RemoteConsentWindow 之前）。
        # 仅当 url 以 http:// 开头才弹；https 与无 scheme 都不弹（无 scheme
        # 默认按 https 处理）。
        if url.lower().startswith("http://"):
            if not _http_insecure_warning(self.win, url):
                # 用户取消 → 关闭启用 checkbox
                self.remote_enabled_var.set(False)
                return False
        # 强制确认（保留三项：合法用途 / 未受骗 / 服务器地址正确）
        if not RemoteConsentWindow(self.win, url).show():
            # 用户取消
            self.remote_enabled_var.set(False)
            return False

        # v1.7.7：启用前做一次连接校验。
        # 只写 enabled=True 的话，上传线程登录失败会持续静默重试，
        # 界面无任何提示。这里先验一次，失败就报错并把 checkbox 复位。
        if REMOTE_CLIENT_AVAILABLE and remote_client:
            try:
                _client = remote_client.RemoteClient(url)
                if not url.lower().startswith("http://"):
                    _store = _client.store
                    if not _store.get_fingerprint(url):
                        messagebox.showerror(
                            "尚未信任服务器",
                            "这台服务器的证书还没信任过。\n\n"
                            "请先点「测试连接」，在弹出的指纹确认框里确认信任，\n"
                            "然后再勾选「启用远程模式」。",
                            parent=self.win)
                        self.remote_enabled_var.set(False)
                        return False
                _client.login_with_key(api_key, client_name)
            except Exception as e:
                messagebox.showerror("启用失败", f"无法连接服务器：\n\n{e}",
                                     parent=self.win)
                self.remote_enabled_var.set(False)
                return False

        # 写回 + 启动
        self.app.config.setdefault("remote", {})
        self.app.config["remote"].update({
            "enabled": True,
            "server_url": url,
            "client_name": client_name,
            "auto_upload": bool(self.remote_auto_var.get()),
        })
        # v1.6.0：API key 加密存（DPAPI/Keychain/机器绑定）
        if needs_save:
            saved = save_remote_key(self.app.config, api_key)
            if not saved:
                messagebox.showwarning(
                    "API Key 无法安全保存",
                    "当前平台不支持安全凭据存储，key 只在本次会话内存中使用。\n"
                    "重启 UCopy 后需要重新输入 key。",
                    parent=self.win)
        try:
            save_config(self.app.config)
        except Exception:
            pass
        self.app.remote_uploader.configure(
            server_url=url,
            api_key=api_key,
            client_id=client_name,
            auto_upload=bool(self.remote_auto_var.get()),
        )
        self.app.remote_uploader.set_enabled(True)
        # 清掉 key 输入框（已加密存盘）
        if needs_save:
            self.remote_key_var.set("")
        try:
            self.app.logger.info(f"[remote] 远程模式已启用: {url} (client={client_name})")
        except Exception:
            pass
        self._refresh_remote_status()
        return True

    def test_remote(self):
        url = self.remote_url_var.get().strip()
        client_name = self.remote_client_name_var.get().strip()
        api_key, needs_save = self._get_remote_key()
        if not url:
            messagebox.showerror("错误", "请填写服务器地址", parent=self.win)
            return
        if not client_name:
            messagebox.showerror("错误", "请填写本机名称", parent=self.win)
            return
        if not api_key:
            messagebox.showerror(
                "错误",
                "请填写 API Key。\n\n"
                "在服务器 Web GUI 登录后 → 「API Keys」卡片 → 创建，\n"
                "复制生成的 key 粘贴到这里。",
                parent=self.win)
            return
        if not REMOTE_CLIENT_AVAILABLE:
            messagebox.showerror("错误", "remote_client 模块不可用",
                                 parent=self.win)
            return
        # v1.7.1：HTTP 明文模式警告（首次）
        if url.lower().startswith("http://"):
            if not _http_insecure_warning(self.win, url):
                return
        # v1.7.2：HTTP 明文协议没证书/指纹，**跳过**整段 cert 探测与首次信任弹窗。
        # 浏览器把 HTTPS-first 当常识，但 UCopy 不强制 TLS —— 内网用户用 HTTP 也得能
        # 测试连接，不能被「无法获取服务器证书指纹」的报错堵住。
        is_http_url = url.lower().startswith("http://")
        client = remote_client.RemoteClient(url)
        fp = None if is_http_url else client.get_current_fingerprint()
        if not is_http_url:
            # HTTPS 路径：第一次自签 cert 会抛 SSLCertVerificationError；先做一次
            # 「探测」拿 fingerprint
            try:
                fp = client.get_current_fingerprint()
            except Exception as e:
                messagebox.showerror("连接失败", str(e), parent=self.win)
                return
            if not fp:
                messagebox.showerror("连接失败", "无法获取服务器证书指纹（地址错？HTTPS 端口错？）",
                                     parent=self.win)
                return
            # 拿 store 里的已有 fingerprint
            store = client.store
            stored = store.get_fingerprint(url)
            if stored is None:
                # 未信任 → 弹窗让用户决定
                ok = messagebox.askyesno(
                    "信任此服务器？",
                    f"服务器证书指纹（SHA-256）：\n\n{fp}\n\n"
                    f"这是自签证书（首次连接）。\n"
                    f"确认信任后该指纹会保存到 ~/.ucopy_trusted_servers.json，\n"
                    f"以后只接受这个指纹（防 MITM）。\n\n"
                    f"是否信任？",
                    parent=self.win,
                )
                if not ok:
                    return
                store.trust(url, fp)
            elif stored != fp:
                # 已有信任但 fingerprint 不一致 → 拒绝
                messagebox.showerror(
                    "证书指纹不匹配",
                    f"当前服务器指纹与已信任的不一致：\n\n"
                    f"已信任: {stored[:32]}…\n"
                    f"现在  : {fp[:32]}…\n\n"
                    f"服务器可能换了证书，或者有人在中间人攻击。\n"
                    f"要信任新证书请先删除 ~/.ucopy_trusted_servers.json 中此 URL 的记录。",
                    parent=self.win,
                )
                return
            # 信任已建立，重新 new client 让它走 CERT_NONE 路径
            client = remote_client.RemoteClient(url, store=store)
        try:
            login_data = client.login_with_key(api_key, client_name)
            me = client.me()
            prev = login_data.get("previous_client_id")
            extra = ""
            if prev:
                extra = f"\n\n⚠️ 已接管原占用设备「{prev}」的连接\n（原设备会收到锁丢失，需要重新连接）"
            messagebox.showinfo(
                "成功",
                f"连接成功！\n"
                f"账号: {me['username']}\n"
                f"角色: {me['role']}\n"
                f"配额: {me['quota_mb']} MB\n"
                f"心跳: 每 {client.heartbeat_interval} 秒{extra}",
                parent=self.win)
            # 同步 UI 上的 fingerprint 显示（v1.7.2：HTTP 明文模式无指纹，写空）
            display_fp = fp or ""
            self.remote_fp_var.set(display_fp)
            # 写回配置
            self.app.config.setdefault("remote", {})
            self.app.config["remote"].update({
                "server_url": url,
                "client_name": client_name,
                "auto_upload": bool(self.remote_auto_var.get()),
                "trusted_fingerprint": display_fp,
            })
            # v1.6.0：API key 加密存储
            if needs_save:
                saved = save_remote_key(self.app.config, api_key)
                if not saved:
                    messagebox.showwarning(
                        "API Key 无法安全保存",
                        "当前平台不支持安全凭据存储，key 只在本次会话内存中使用。\n"
                        "重启 UCopy 后需要重新输入 key。",
                        parent=self.win)
                else:
                    self.remote_key_var.set("")  # 已存盘，清输入框
            try:
                save_config(self.app.config)
            except Exception:
                pass
            # 让 uploader 拿到 client + key
            self.app.remote_uploader.configure(
                server_url=url, api_key=api_key, client_id=client_name,
                auto_upload=bool(self.remote_auto_var.get()),
            )
            self.app.remote_uploader.client = client
            self.app.remote_uploader.key_lock_lost = False
            # 启动心跳（保持独占锁）
            self.app.remote_uploader._start_heartbeat()
        except remote_client.RemoteError as e:
            code = getattr(e, "code", "") or ""
            if code == "key_lock_lost":
                messagebox.showerror(
                    "Key 被占用",
                    "该 key 已被其他设备占用。\n\n"
                    "每个 key 同一时间只能被一台客户端使用。\n"
                    "要去占用其他设备，请在服务器 Web GUI 的「API Keys」里点「踢下线」，\n"
                    "或等对方 2 分钟无心跳后自动释放。",
                    parent=self.win)
            elif code in ("unauthenticated", "invalid_key"):
                messagebox.showerror(
                    "连接失败",
                    "API Key 无效。\n\n"
                    "请在服务器 Web GUI 的「API Keys」卡片重新创建 key，\n"
                    "并确认复制的是完整的 key（含中间的「.」）。",
                    parent=self.win)
            else:
                messagebox.showerror("连接失败",
                                     f"{f'[{code}] ' if code else ''}{e.message}",
                                     parent=self.win)
        except Exception as e:
            messagebox.showerror("连接失败", str(e), parent=self.win)

    def pull_remote_config(self):
        """从服务器拉取配置（覆盖本地 security.* 之外的所有字段）。"""
        url = self.remote_url_var.get().strip()
        client_name = self.remote_client_name_var.get().strip()
        api_key, _ = self._get_remote_key()
        if not (url and client_name and api_key):
            messagebox.showerror("错误", "请填写服务器地址 / 本机名称 / API Key",
                                 parent=self.win)
            return
        try:
            client = remote_client.RemoteClient(url)
            client.login_with_key(api_key, client_name)
            remote_cfg = client.fetch_config()
            if not remote_cfg:
                messagebox.showinfo("提示", "服务器上还没有保存任何配置",
                                    parent=self.win)
                return
            # 保留本地 security（不要被覆盖）
            local_sec = self.app.config.get("security", {}) or {}
            new_cfg = {**remote_cfg, "security": local_sec}
            # 不要覆盖 remote 段（server_url / enabled / 等是本地的）
            new_cfg["remote"] = self.app.config.get("remote", {})
            self.app.config = new_cfg
            save_config(self.app.config)
            self.app.remote_uploader.configure(
                server_url=url, api_key=api_key, client_id=client_name,
                auto_upload=bool(self.remote_auto_var.get()),
            )
            self.app.remote_uploader.client = client
            messagebox.showinfo("成功", "已从服务器拉取配置（密码/密保保留本地）",
                                parent=self.win)
            try:
                self.app.config["remote"]["last_config_sync_at"] = (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                save_config(self.app.config)
            except Exception:
                pass
        except remote_client.RemoteError as e:
            messagebox.showerror("拉取失败",
                                 f"{f'[{e.code}] ' if e.code else ''}{e.message}",
                                 parent=self.win)

    def push_remote_config(self):
        """把本地配置推送到服务器。"""
        url = self.remote_url_var.get().strip()
        client_name = self.remote_client_name_var.get().strip()
        api_key, _ = self._get_remote_key()
        if not (url and client_name and api_key):
            messagebox.showerror("错误", "请填写服务器地址 / 本机名称 / API Key",
                                 parent=self.win)
            return
        if not messagebox.askyesno("确认推送",
                                    "推送会把当前本地配置写到服务器。\n"
                                    "服务器上的旧配置将被覆盖。\n"
                                    "确定吗？",
                                    parent=self.win):
            return
        try:
            client = remote_client.RemoteClient(url)
            client.login_with_key(api_key, client_name)
            info = client.push_config(self.app.config)
            self.app.remote_uploader.configure(
                server_url=url, api_key=api_key, client_id=client_name,
                auto_upload=bool(self.remote_auto_var.get()),
            )
            self.app.remote_uploader.client = client
            try:
                self.app.config["remote"]["last_config_sync_at"] = (
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                save_config(self.app.config)
            except Exception:
                pass
            messagebox.showinfo("成功",
                                f"已推送（version {info.get('version')}）",
                                parent=self.win)
        except remote_client.RemoteError as e:
            messagebox.showerror("推送失败",
                                 f"{f'[{e.code}] ' if e.code else ''}{e.message}",
                                 parent=self.win)

    def on_mode_changed(self, event=None):
        """
        切换黑/白名单模式时：
          1. 立刻把模式写回 app.config 并落盘
          2. 同步刷新按钮文字、列表状态标记和说明

        以前只改了 UI 文案，app.config 里的 device_list_mode 没变，
        而 U 盘插入的复制判定读的是 app.config —— 于是「切到白名单后
        不点保存就插 U 盘」仍然按黑名单执行，看起来就是白名单不工作。
        """
        mode = normalize_list_mode(self.list_mode_var.get())
        noun = list_mode_noun(mode)
        if mode == "blacklist":
            hint = "黑名单：名单内的 U 盘不复制，其余都复制；切换模式会立即生效"
            self.toggle_btn.config(text="加入/移出黑名单")
        else:
            hint = "白名单：只有名单内的 U 盘才复制，其余都不复制；切换模式会立即生效"
            self.toggle_btn.config(text="加入/移出白名单")
        self.mode_hint_var.set(hint)

        # 立即生效：写回配置并落盘，不依赖「保存并隐藏」
        if self.app.config.get("device_list_mode") != mode:
            self.app.config["device_list_mode"] = mode
            save_config(self.app.config)
            try:
                self.app.logger.info(f"名单模式已切换为{LIST_MODE_LABELS[mode]}，立即生效")
            except Exception:
                pass
        self.refresh_device_list()

    def browse_target(self):
        folder = filedialog.askdirectory(parent=self.win)
        if folder:
            self.target_var.set(folder)

    def refresh_device_list(self):
        self.device_listbox.delete(0, tk.END)
        mode = normalize_list_mode(self.list_mode_var.get())
        noun = list_mode_noun(mode)
        known = self.app.config.get("known_devices", {})
        for uid, info in known.items():
            label = info.get("label", "?")
            listed = bool(info.get("listed", False))
            if listed:
                tag = f"[在{noun}中]"
            else:
                tag = "[会复制]" if mode == "blacklist" else "[不复制]"
            self.device_listbox.insert(tk.END, f"{label}  [{uid}]  {tag}")

    def toggle_listed(self):
        selected = self.device_listbox.curselection()
        if not selected:
            messagebox.showinfo("提示", "请先选择U盘", parent=self.win)
            return
        known = self.app.config.get("known_devices", {})
        items = list(known.items())
        for idx in selected:
            if idx < len(items):
                uid, info = items[idx]
                info["listed"] = not bool(info.get("listed", False))
        self.app.config["device_list_mode"] = normalize_list_mode(self.list_mode_var.get())
        save_config(self.app.config)
        self.refresh_device_list()

    def toggle_blacklist(self):
        """旧方法名，保留为 toggle_listed 的别名以兼容外部调用。"""
        return self.toggle_listed()

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
            "device_list_mode": normalize_list_mode(self.list_mode_var.get()),
        }
        save_config(cfg)
        self.app.config = cfg
        set_auto_start(cfg["auto_start"])
        mode = cfg["device_list_mode"]
        self.app.logger.info(f"设置已保存（{LIST_MODE_LABELS[mode]}）")
        self.on_close()

    def on_close(self):
        try:
            self.win.destroy()
        finally:
            if self.on_close_cb:
                self.on_close_cb()

    def change_password(self):
        ChangePasswordWindow(self.win, self.app)

    def change_qa(self):
        ChangeQAWindow(self.win, self.app)

if __name__ == "__main__":
    app = UCopyApp()
    app.run()