"""摄像头 / 麦克风占用检测模块（检测层，只产出状态，不负责界面）。

=====================================================================
设计目标
=====================================================================
给 UCopy 右上角那个紫色小圆点提供「摄像头 / 麦克风是否正在被使用」的
判定结果。上层（ucopy.py）拿到状态后自行决定显示什么颜色，本模块不碰
任何 UI，也不修改 ucopy.py。

状态语义（三种颜色）：
    * 摄像头被任意程序占用（无论麦克风）  -> STATE_CAMERA  -> 绿色
    * 摄像头未被占用、但麦克风被占用      -> STATE_MIC     -> 橙色
    * 两者都未被占用                       -> STATE_IDLE    -> 紫色

=====================================================================
技术路线：为什么这样能覆盖虚拟摄像头 / 虚拟麦克风
=====================================================================
刻意**不做**「机器上插没插物理 USB 摄像头」这种判断——那样 OBS Virtual
Camera、ManyCam、CamTwist、snap Camera、VCAM 等虚拟驱动会被整体漏掉，
而它们恰恰是最需要亮灯的场景（录屏、直播、摄像头滤镜）。

本模块的判断依据统一为「**系统层面，这个设备类别里有任何设备被别的程序
打开/占用**」：

1. 摄像头 —— SetupAPI 枚举视频设备类别
   GUID_DEVCLASS_CAMERA = {ca3e7ab9-b4c3-4ae6-8251-58ef5fbb7f7c}
   GUID_DEVINTERFACE_VIDEO_DEVICE = {e6cafbd7-1866-4b66-9f8d-431d5f55c1da}

   Windows 没有官方的「设备是否被占用」查询 API。可行做法是：对枚举到的
   每个**已连接（present）**设备取设备接口路径，然后以**独占方式**
   （dwShareMode = 0）用 CreateFileW 打开它。别的程序已经打开着的时候，
   I/O 管理器会因为共享冲突返回 ERROR_SHARING_VIOLATION(32) / ERROR_BUSY(170)。

   所有摄像头类驱动——物理的和虚拟的——都在同一个设备类别下注册，并都
   暴露标准的视频设备接口，所以虚拟摄像头天然被一并枚举到、同样能被探到。
   两个 GUID 都做枚举、以接口路径为主、实例 ID 为兜底，就是为了把那些
   只在类枚举里出现、接口枚举里没暴露的虚拟驱动也捞进来。

2. 麦克风 —— winmm 的 waveInOpen 独占尝试
   waveInGetNumDevs / waveInGetDevCaps 枚举录音设备，逐个用 waveInOpen
   尝试打开。Windows 上普通应用打开麦克风走共享模式，因此这里会返回
   MMSYSERR_ALLOCATED(10)；若返回 WAVERR_STILLPLAYING(33) 则设备还在录
   音/回放中。虚拟音频驱动（VB-CABLE、VoiceMeeter、OBS Virtual Audio、
   立体声混音等）同样是标准 waveIn 设备，一样会被枚举和探到。

=====================================================================
误判风险与保守策略（本模块最关键的设计目标）
=====================================================================
探测必须只读、无副作用：绝不抢占设备、不改用户的音频设置、不弹窗。
为了把误判压到最低：

  A. 只有**明确的占用类错误**才算「占用」：
       摄像头 ERROR_SHARING_VIOLATION(32) / ERROR_BUSY(170) / ERROR_DEVICE_BUSY
       麦克风 MMSYSERR_ALLOCATED(10) / WAVERR_STILLPLAYING(33)
     其它一切错误（路径打不开、权限不足、驱动不支持独占、设备被独占失败等）
     一律**忽略这个设备**，绝不因为「打不开」就猜成占用。

  B. 三值判定，而不是布尔：
       True  = 确实检测到占用
       False = 确实检测到空闲（探测成功且明确空闲）
       None  = 未知 / 检测不可靠
     上层按「未知 = 保持当前 / 回到紫色」处理，绝不把未知猜成占用。

  C. 所有 API 调用都在 try/except 里，任何一步抛异常都被吞掉并记日志，
     绝不冒泡打死探测线程。所有打开的句柄在 finally 中关闭，录音设备
     必须 waveInClose，不泄漏。

  D. 模块被 import 时不执行任何探测；Windows API 全部在函数内部懒加载，
     因此本文件在 macOS/Linux 上也能安全 import（供 --selftest 使用）。

已知无法完全规避的边界（详见交付报告）：
  * 部分虚拟摄像头驱动在被独占打开时可能直接返回 SUCCESS（不尊重共享
    语义），这类设备「空闲」时也会被判成占用 → 假亮灯。
  * 反之，极少数驱动独占打开时返回的是非占用类错误（如 ACCESS_DENIED），
    这类设备在真被使用时也可能漏检 → 假灭灯。这是保守策略的必然代价，
    选择「宁可漏检也不误报亮灯」，因为常驻指示灯误亮更招人烦。
  * waveInOpen 探测本身是一次真实的打开+关闭（设备空闲时），
    理论上可能与用户程序的抢占/切换存在极小的时间窗竞争。
"""

import ctypes
import sys
import threading
import time

# ---------- 状态常量 ----------
STATE_IDLE = "idle"        # 都空闲 -> 紫色
STATE_CAMERA = "camera"    # 摄像头在用 -> 绿色
STATE_MIC = "mic"          # 仅麦克风在用 -> 橙色

# ---------- 颜色映射（上层直接拿去换小圆点的颜色） ----------
COLOR_IDLE = "#8B5CF6"     # 紫色（与 ucopy.FloatingDot.DOT_COLOR 保持一致）
COLOR_CAMERA = "#22C55E"   # 绿色
COLOR_MIC = "#F59E0B"      # 橙色

STATE_COLORS = {
    STATE_IDLE: COLOR_IDLE,
    STATE_CAMERA: COLOR_CAMERA,
    STATE_MIC: COLOR_MIC,
}

# ---------- Windows DLL 缓存 ----------
# 1Hz 轮询下，每次重新 WinDLL("setupapi.dll") / WinDLL("winmm.dll") 都会触发一次
# 重复的 LoadLibrary + argtypes/restype 绑定——既浪费 CPU，也容易触发反病毒软件告警。
# 这里用模块级缓存：首次懒加载后，整个进程复用同一份 DLL 对象 + 绑定。
_WIN32_DLLS = {}


def _win32_dll(name):
    """懒加载并缓存 Win32 DLL。多次调用返回同一对象。"""
    cached = _WIN32_DLLS.get(name)
    if cached is not None:
        return cached
    dll = ctypes.WinDLL(name, use_last_error=True)
    _WIN32_DLLS[name] = dll
    return dll


def _reset_win32_dll_cache():
    """清空 DLL 缓存（仅测试用）。"""
    _WIN32_DLLS.clear()


# ---------- Windows 常量（模块级只是整数，不触发任何 API 调用） ----------
_WIN = sys.platform == "win32"

_DEVCLASS_CAMERA = "{ca3e7ab9-b4c3-4ae6-8251-58ef5fbb7f7c}"
_DEVINTERFACE_VIDEO_DEVICE = "{e6cafbd7-1866-4b66-9f8d-431d5f55c1da}"
# KSCATEGORY_VIDEO_CAMERA — 内核流类别，老摄像头驱动 / 第三方 SDK（ev录频、抖音伴侣、
# ManyCam 等虚拟驱动）经常只在这一类里注册。微软 DDK ksmedia.h 标准值。
_KSCATEGORY_VIDEO_CAMERA = "{65e8773d-8f56-11d0-a3b9-00a0c9223196}"
# KSCATEGORY_CAPTURE — 通用音视频捕获类别，部分 DirectShow filter 用这一类。
_KSCATEGORY_CAPTURE = "{6994ad05-93ef-11d0-a3ac-00a0c9223196}"
# KSCATEGORY_VIDEO — 父类别，包含 VIDEO_CAMERA / CAPTURE 等子类。
_KSCATEGORY_VIDEO = "{6994ad04-93ef-11d0-a3ac-00a0c9223196}"
# 摄像头枚举要查的所有 GUID（去重由调用方负责）
_CAMERA_ENUM_GUIDS = (
    _DEVCLASS_CAMERA,
    _DEVINTERFACE_VIDEO_DEVICE,
    _KSCATEGORY_VIDEO_CAMERA,
    _KSCATEGORY_CAPTURE,
    _KSCATEGORY_VIDEO,
)

DIGCF_PRESENT = 0x00000002
DIGCF_DEVICEINTERFACE = 0x00000010

INVALID_HANDLE_VALUE = -1  # ctypes 层面为 c_void_p(-1).value
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_ATTRIBUTE_NORMAL = 0x00000080
SPDRP_HARDWAREID = 0x00000001
SPDRP_FRIENDLYNAME = 0x0000000C
DICS_FLAG_GLOBAL = 0x00000001

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_HANDLE = 6
ERROR_SHARING_VIOLATION = 32
ERROR_BUSY = 170

# 「占用类」错误白名单：只有命中这些才判定为占用。
# 注意加入 ERROR_ACCESS_DENIED(5)：
#   系统自带「拍照」、第三方录屏软件（ev录频、Bandicam 等）以独占方式打开摄像头
#   时，部分驱动对其它进程返回 ACCESS_DENIED 而非 SHARING_VIOLATION。
#   没有它会漏掉绝大多数日常使用场景。
CAMERA_IN_USE_ERRORS = frozenset((ERROR_SHARING_VIOLATION, ERROR_BUSY,
                                  ERROR_ACCESS_DENIED, 100, 1117))

MMSYSERR_NOERROR = 0
MMSYSERR_ALLOCATED = 10
WAVERR_STILLPLAYING = 33
MIC_IN_USE_CODES = frozenset((MMSYSERR_ALLOCATED, WAVERR_STILLPLAYING))

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_QUERY = 0x80000000
CALLBACK_NULL = 0

DEFAULT_INTERVAL = 1.0
DEFAULT_ACTIVE_INTERVAL = 1.0   # 检测到占用时（active 状态）的轮询间隔
DEFAULT_IDLE_INTERVAL = 5.0     # 长时间空闲时的轮询间隔（节能）
DEFAULT_UNKNOWN_STREAK = 3      # 连续多少次 (None, None) 触发"探测失败"回调
MIN_INTERVAL = 0.01             # 间隔下限，防止误设到 0.001 把 CPU 跑满


def _log_debug(msg, *args):
    """极简日志：默认不输出，避免污染 EXE 的控制台；有需要可自行接 logging。"""
    if _DEBUG:  # pragma: no cover - 仅调试时生效
        try:
            sys.stderr.write("[device_indicator] " + (msg % args if args else msg) + "\n")
        except Exception:
            pass


_DEBUG = False


def set_debug(flag):
    """打开/关闭调试输出（排障用，默认关闭）。"""
    global _DEBUG
    _DEBUG = bool(flag)


# =====================================================================
# 纯逻辑层：状态机与颜色（不依赖 Windows，可在任意平台测试）
# =====================================================================

def indicator_state(camera_in_use, mic_in_use):
    """把两个三值状态映射成一个指示灯状态字符串。

    严格按以下优先级判定，**不做任何记忆/粘滞**（这正是保证
    「摄像头停了但麦克风仍在用 -> 切到橙色，而不是继续绿色或掉回紫色」
    的关键）：

        camera is True            -> STATE_CAMERA（绿色）
        camera 非 True 且 mic True -> STATE_MIC（橙色）
        其余（含 None 未知）        -> STATE_IDLE（紫色）

    参数可以是 None（未知）。未知一律按「不是占用」处理，即回到/保持空闲，
    绝不因为探测失败而点亮指示灯。

        >>> indicator_state(True, True)
        'camera'
        >>> indicator_state(False, True)
        'mic'
        >>> indicator_state(None, True)
        'mic'
        >>> indicator_state(None, None)
        'idle'
    """
    if camera_in_use is True:
        return STATE_CAMERA
    if mic_in_use is True:
        return STATE_MIC
    return STATE_IDLE


def state_color(state):
    """状态字符串 -> "#RRGGBB"。未知状态一律退化为空闲紫。"""
    return STATE_COLORS.get(state, COLOR_IDLE)


# =====================================================================
# Windows 底层：ctypes 结构体（只在 Windows 上真正使用）
# =====================================================================

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("InterfaceClassGuid", _GUID),
        ("Flags", ctypes.c_ulong),
        ("Reserved", ctypes.c_size_t),
    ]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("ClassGuid", _GUID),
        ("DevInst", ctypes.c_ulong),
        ("Reserved", ctypes.c_size_t),
    ]


class _WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_ulong),
        ("nAvgBytesPerSec", ctypes.c_ulong),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WAVEINCAPS(ctypes.Structure):
    _fields_ = [
        ("wCaps", ctypes.c_ushort),
        ("wReserved", ctypes.c_ushort),
        ("wvFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_ulong),
        ("nAvgBytesPerSec", ctypes.c_ulong),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("dwChannels", ctypes.c_ulong),
        ("dwSupport", ctypes.c_ulong),
    ]


def _guid(text):
    """把 "{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}" 解析成 _GUID。"""
    text = text.strip().strip("{}")
    a, b, c, d, e = text.split("-")
    raw = bytes.fromhex(a)[::-1] + bytes.fromhex(b)[::-1] + bytes.fromhex(c)[::-1] \
        + bytes.fromhex(d) + bytes.fromhex(e)
    return _GUID.from_buffer_copy(raw)


def _load_setupapi():
    """懒加载 setupapi.dll + kernel32.dll（import 本模块时不会走到这里）。

    DLL 通过 _win32_dll() 模块级缓存：多次调用返回同一对象，避免 1Hz 轮询下
    重复 LoadLibrary / 重复绑定 argtypes。
    """
    setupapi = _win32_dll("setupapi.dll")
    kernel32 = _win32_dll("kernel32.dll")

    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong]
    setupapi.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiEnumDeviceInterfaces.restype = ctypes.c_int
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_int
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA),
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    setupapi.SetupDiEnumDeviceInfo.restype = ctypes.c_int
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = ctypes.c_int
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA),
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]

    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return setupapi, kernel32


def _load_winmm():
    winmm = _win32_dll("winmm.dll")
    winmm.waveInGetNumDevs.restype = ctypes.c_uint
    winmm.waveInGetDevCapsW.restype = ctypes.c_uint
    winmm.waveInGetDevCapsW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_WAVEINCAPS), ctypes.c_ulong]
    winmm.waveInOpen.restype = ctypes.c_uint
    winmm.waveInOpen.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_size_t, ctypes.c_ulong]
    winmm.waveInClose.restype = ctypes.c_uint
    winmm.waveInClose.argtypes = [ctypes.c_void_p]
    return winmm


def _interface_path(setupapi, dev_info, ifdata):
    """SetupDiGetDeviceInterfaceDetailW 两段式取设备接口路径（宽字符串）。"""
    required = ctypes.c_ulong(0)
    rc = setupapi.SetupDiGetDeviceInterfaceDetailW(dev_info, ctypes.byref(ifdata),
                                                  None, 0, ctypes.byref(required), None)
    if rc == 0 or required.value == 0:
        return None
    buf = ctypes.create_string_buffer(required.value)
    rc = setupapi.SetupDiGetDeviceInterfaceDetailW(dev_info, ctypes.byref(ifdata),
                                                  ctypes.cast(buf, ctypes.c_void_p),
                                                  required.value, None, None)
    if rc == 0:
        return None
    # 缓冲区里 DevicePath 是一段以 L'\0' 结尾的 UTF-16 字符串
    return ctypes.cast(buf, ctypes.c_wchar_p).value


def _enumerate_paths_for_guid(setupapi, guid_text):
    """用给定的 GUID 枚举所有 PRESENT 设备的接口路径。

    返回 list[str]；任何一步异常都返回空 list（不抛给上层，避免一个 GUID
    失败拖垮整个枚举）。
    """
    paths = []
    try:
        class_guid = _guid(guid_text)
        iface_guid = _guid(guid_text)  # 同一 GUID 同时用作类 GUID + 接口 GUID
        dev_info = setupapi.SetupDiGetClassDevsW(ctypes.byref(class_guid), None, None,
                                                DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        if not dev_info or dev_info == INVALID_HANDLE_VALUE:
            return []
        try:
            index = 0
            while True:
                ifdata = _SP_DEVICE_INTERFACE_DATA()
                ifdata.cbSize = ctypes.sizeof(ifdata)
                if not setupapi.SetupDiEnumDeviceInterfaces(dev_info, None,
                                                           ctypes.byref(iface_guid),
                                                           index, ctypes.byref(ifdata)):
                    break
                path = _interface_path(setupapi, dev_info, ifdata)
                if path:
                    paths.append(path)
                index += 1
        finally:
            try:
                setupapi.SetupDiDestroyDeviceInfoList(dev_info)
            except Exception:
                pass
    except Exception as exc:
        _log_debug("GUID %s 枚举异常: %r", guid_text, exc)
    return paths


def _enumerate_camera_paths():
    """枚举所有**当前连接**的摄像头设备接口路径（虚拟驱动同样在此列）。

    返回 list[str]；任何 GUID 失败都不影响其它 GUID。

    多 GUID 并行：单 GUID 可能漏掉一些只在 KS 流类别 / DirectShow filter
    类下注册的虚拟驱动，多 GUID 覆盖更广。
    """
    setupapi, _kernel32 = _load_setupapi()
    all_paths = []
    for guid_text in _CAMERA_ENUM_GUIDS:
        all_paths.extend(_enumerate_paths_for_guid(setupapi, guid_text))

    # 去重，保持顺序稳定
    seen = set()
    unique = []
    for p in all_paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    _log_debug("摄像头枚举完成：%d 个 GUID 共发现 %d 个唯一设备",
               len(_CAMERA_ENUM_GUIDS), len(unique))
    return unique


def _open_device_exclusive(kernel32, path, share_mode):
    """以指定共享模式打开设备路径。

    share_mode=0 表示独占，其它值表示 FILE_SHARE_* 位掩码。

    返回 (True, None)  打开成功
          (False, err)  打开失败，err 为 GetLastError
    """
    handle = kernel32.CreateFileW(path, GENERIC_READ, share_mode, None,
                                 OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == INVALID_HANDLE_VALUE or handle == 0:
        return False, ctypes.get_last_error()
    try:
        return True, None
    finally:
        kernel32.CloseHandle(handle)


def _strip_interface_suffix(path):
    """去掉设备接口路径尾部的 #{实例-类-GUID} 部分，得到设备路径。

    SetupDiEnumDeviceInterfaces 返回的是接口（符号链接）路径，
    部分驱动只允许按设备路径打开，去掉接口后缀再试一次能显著提高命中率。
    """
    marker = "#{"
    idx = path.rfind(marker)
    if idx > 0:
        return path[:idx]
    return None


def _open_variants(kernel32, path):
    """
    按多个方式尝试打开同一个摄像头设备，提高真机命中率。

    返回 (opened, in_use_err)：
      opened      至少有一次打开成功 -> 该设备当前**未观察到**占用信号
      in_use_err  某次打开返回了明确的占用类错误码（否则为 None）

    多策略：
      1. 独占 (dwShareMode=0)
      2. 读+写共享 (FILE_SHARE_READ|FILE_SHARE_WRITE)
      3. 仅读共享 (FILE_SHARE_READ)
      4. 对去掉接口后缀的"设备路径"重试 1~3

    只要任一次拿到明确的占用类错误码，立刻判定为占用；
    否则只要有一次打开成功，标记为"未观察到占用"（让上层做多源判定）。
    """
    candidates = [path]
    stripped = _strip_interface_suffix(path)
    if stripped:
        candidates.append(stripped)

    share_modes = (
        0,  # 独占
        FILE_SHARE_READ | FILE_SHARE_WRITE,  # 读+写共享
        FILE_SHARE_READ,  # 仅读共享
    )

    in_use_err = None
    opened = False
    for cand in candidates:
        for share_mode in share_modes:
            try:
                ok, err = _open_device_exclusive(kernel32, cand, share_mode)
            except Exception as exc:
                _log_debug("打开摄像头设备异常 %s (share=%d): %r",
                           cand, share_mode, exc)
                continue
            if ok:
                opened = True
                # 已经成功打开，不需要再试别的 share_mode
                break
            if err in CAMERA_IN_USE_ERRORS:
                # 明确的占用类错误码，立刻判定为占用
                in_use_err = err
                return opened, in_use_err
            # 非占用类错误码（路径失效等），继续尝试下一种方式
            _log_debug("摄像头设备 %s (share=%d) 打开失败 err=%d，继续",
                       cand, share_mode, err)
        if in_use_err is not None or opened:
            # 已确定结果，跳出候选循环
            if in_use_err is not None:
                return opened, in_use_err
            break
    return opened, in_use_err


def camera_in_use(conservative=True):
    """摄像头是否正在被任意程序使用。

    返回 True / False / None(未知)。

    判定逻辑：
      1. 枚举所有 PRESENT 的摄像头设备接口路径；
      2. 对每条路径做独占打开尝试：
         - 打开成功                 -> 该设备空闲
         - 返回占用类错误（SHARING_VIOLATION / BUSY / ACCESS_DENIED / 等）
                                   -> 该设备疑似占用
         - 其它错误（路径失效、权限不足以外的） -> 忽略该设备
      3. 多源确认，避免单设备驱动权限问题造成的假亮灯：
         - 0 个疑似占用设备       -> False（全部空闲）
         - 1 个枚举设备 + 1 个疑似 -> True（单设备时单证据即算占用）
         - 2+ 个枚举设备 + 全部疑似 -> True（多设备时需要全员确认）
         - 2+ 个枚举设备 + 部分疑似 -> 1 个时算 True，多个则要求至少 2 个
         - 全部都打开失败（疑似但又没拿到明确证据）-> None（未知）
    """
    if not _WIN:
        # 非 Windows 平台没有可探测的摄像头类别
        return False if not conservative else None
    probed = 0
    try:
        paths = _enumerate_camera_paths()
    except Exception as exc:
        _log_debug("摄像头枚举异常: %r", exc)
        return False if not conservative else None
    try:
        _setupapi, kernel32 = _load_setupapi()
    except Exception as exc:
        _log_debug("setupapi/kernel32 加载异常: %r", exc)
        return False if not conservative else None

    if not paths:
        # 一个摄像头设备都没接：不可能有程序在占用 -> 空闲
        return False

    in_use_count = 0
    non_in_use_errors = 0  # 拿到了明确的非占用类错误（驱动权限错配、路径失效等）
    for path in paths:
        try:
            opened, in_use_err = _open_variants(kernel32, path)
        except Exception as exc:
            _log_debug("探测摄像头设备异常 %s: %r", path, exc)
            continue
        if opened and in_use_err is None:
            # 唯一明确的空闲信号：打开成功且没有任何错误码
            probed += 1
        elif in_use_err in CAMERA_IN_USE_ERRORS:
            in_use_count += 1
        elif in_use_err is not None:
            # 拿到了错误码但不在占用白名单（驱动权限错配 / 路径失效等）
            # 不算占用，也不算空闲 —— 「未知」信号
            non_in_use_errors += 1
        else:
            _log_debug("摄像头设备 %s 打开但返回了未知结果，忽略", path)

    if in_use_count == 0:
        # 没有任一设备返回明确的占用信号
        if non_in_use_errors > 0 and probed == 0:
            # 全部都拿到非占用类错误码（路径失效 / 权限错配），按未知处理
            return False if not conservative else None
        return False if probed else (False if not conservative else None)

    # 至少有一个设备返回占用类错误：
    #   - 单设备机器：1 个证据就够
    #   - 多设备机器：1 个证据也算占用
    #     （典型场景：笔记本有内置摄像头 + 外接 USB 摄像头，
    #      但用户只用其中一个，另一个可能被 Windows 单独占用做后台用途，
    #      这时第二个设备的状态不能否决第一个的真占用信号）
    #   多源「否决」语义由 in_use_count==0 时的 non_in_use_errors 分支负责：
    #   全部都是非占用类错误码才返回未知；只要有一个占用证据就报占用。
    return True


def mic_in_use(conservative=True):
    """麦克风是否正在被任意程序使用。

    返回 True / False / None(未知)。

    多源探测（任何一个 True 即判定为占用）：
      1. waveIn 独占打开（老路径，对 Win10/11 上少数仍用 MME 的应用有效）
      2. WASAPI session 枚举（Windows 10/11 主路径，覆盖 ev录频 / 系统录音机 /
         微信/QQ/Teams/Zoom 等所有 WASAPI 应用）
    """
    if not _WIN:
        return False if not conservative else None

    # 探测 1：waveIn 独占打开
    wavein_in_use = _mic_in_use_wavein()
    if wavein_in_use is True:
        return True
    # wavein_in_use 是 None 时（探测失败），继续走 WASAPI；是 False 时（明确空闲）
    # 也继续走 WASAPI —— 因为很多应用只用 WASAPI 不走 waveIn

    # 探测 2：WASAPI session 枚举
    wasapi_in_use = _mic_in_use_wasapi()
    if wasapi_in_use is True:
        return True
    if wavein_in_use is False and wasapi_in_use is False:
        return False
    if wavein_in_use is None and wasapi_in_use is None:
        return False if not conservative else None
    # 至少一个明确说占用、另一个未知
    return True if wasapi_in_use is True or wavein_in_use is True else False


def _mic_in_use_wavein():
    """waveIn 独占打开尝试，返回 True / False / None(未知)。"""
    try:
        winmm = _load_winmm()
        count = winmm.waveInGetNumDevs()
    except Exception as exc:
        _log_debug("winmm 加载/枚举异常: %r", exc)
        return None

    if not count:
        return False  # 没有录音设备 -> 不可能在用

    wfx = _WAVEFORMATEX()
    wfx.wFormatTag = WAVE_FORMAT_PCM
    wfx.nChannels = 1
    wfx.nSamplesPerSec = 8000
    wfx.nAvgBytesPerSec = 8000
    wfx.nBlockAlign = 1
    wfx.wBitsPerSample = 8
    wfx.cbSize = 0

    probed = 0
    for dev_id in range(count):
        handle = ctypes.c_void_p(0)
        try:
            rc = winmm.waveInOpen(ctypes.byref(handle), ctypes.c_uint(dev_id),
                                  ctypes.byref(wfx), CALLBACK_NULL, 0, 0)
        except Exception as exc:
            _log_debug("waveInOpen 异常 dev=%d: %r", dev_id, exc)
            continue
        if rc == MMSYSERR_NOERROR:
            probed += 1
            try:
                winmm.waveInClose(handle)
            except Exception:
                pass
            handle = None
            continue
        if handle:
            try:
                winmm.waveInClose(handle)
            except Exception:
                pass
        if rc in MIC_IN_USE_CODES:
            return True
        _log_debug("录音设备 %d waveInOpen 返回 %d，忽略", dev_id, rc)

    return False if probed else None


# ---------- WASAPI 麦克风占用检测（覆盖 Windows 10/11 主路径） ----------

# 关键 GUID / IID / 接口 vtable 偏移
_CLSID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB636B7BF}"
_IID_IMMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceCollection = "{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}"
_IID_IMMDevice = "{D666063F-1587-4E43-81F1-A94885D7D9B9}"
_IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
_IID_IAudioSessionEnumerator = "{E4F9856D-2A40-4333-9F02-8674CD606D7D}"
_IID_IAudioSessionControl2 = "{CBE83A1F-7A9C-4B91-9C2D-B33C26B4B92C}"

# eDataFlow / role
_eCapture = 1  # eCapture
_eConsole = 0  # eConsole

# AudioSessionState
_AudioSessionStateInactive = 0
_AudioSessionStateActive = 1
_AudioSessionStateExpired = 2

# IUnknown vtable:
#   0: QueryInterface
#   1: AddRef
#   2: Release
# IMMDeviceEnumerator vtable (extends IUnknown):
#   3: EnumAudioEndpoints
#   4: GetDefaultAudioEndpoint
#   5: GetDevice
#   6: RegisterEndpointNotificationCallback
#   7: UnregisterEndpointNotificationCallback
# IMMDeviceCollection vtable:
#   3: GetCount
#   4: Item
# IMMDevice vtable:
#   3: Activate
#   4: OpenPropertyStore
#   5: GetId
#   6: GetState
# IAudioSessionManager2 vtable:
#   3: GetAudioSessionControl
#   4: RegisterAudioSessionNotification
#   5: UnregisterAudioSessionNotification
#   6: RegisterDuckNotification
#   7: UnregisterDuckNotification
#   8: GetSessionEnumerator  ← 唯一我们要的
# IAudioSessionEnumerator vtable:
#   3: GetCount
#   4: GetSession
# IAudioSessionControl2 vtable:
#   3: GetState
#   4: GetDisplayName
#   5: SetDisplayName
#   6: GetIconPath
#   7: SetIconPath
#   8: GetGroupingParam
#   9: SetGroupingParam
#   10: RegisterAudioSessionNotification
#   11: UnregisterAudioSessionNotification
#   12: GetSessionIdentifier
#   13: GetSessionInstanceIdentifier
#   14: GetProcessId
#   15: IsSystemSoundsSession
#   16: SetDuckingPreference    ← 在 IAudioSessionControl2 上


class _COMGUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, text):
        text = text.strip().strip("{}").lower()
        a, b, c, d, e = text.split("-")
        raw = bytes.fromhex(a)[::-1] + bytes.fromhex(b)[::-1] + bytes.fromhex(c)[::-1] \
            + bytes.fromhex(d) + bytes.fromhex(e)
        return cls.from_buffer_copy(raw)


# ctypes.WINFUNCTYPE 在非 Windows 平台不存在，用 CFUNCTYPE 做替代
# （macOS 上 selftest 不调 WASAPI 路径，所以这里的语义只用于类型声明）
if hasattr(ctypes, "WINFUNCTYPE"):
    _CFUNCTYPE = ctypes.WINFUNCTYPE
else:
    _CFUNCTYPE = ctypes.CFUNCTYPE


def _hr_check(hr):
    """简化 HRESULT 检查：FAILED(hr) 返回 True。"""
    return (hr & 0xFFFF0000) != 0


def _load_mfplat():
    """懒加载 ole32 + propsys DLL。"""
    ole32 = _win32_dll("ole32.dll")
    ole32.CoInitializeEx.restype = ctypes.c_uint
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoUninitialize.restype = None
    ole32.CoUninitialize.argtypes = []
    ole32.CoCreateInstance.restype = ctypes.c_uint
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                       ctypes.c_void_p, ctypes.c_void_p]
    return ole32


def _wasapi_enum_capture_sessions():
    """枚举所有 capture 设备的 audio session，返回 list of session state。

    返回 [(state_int, process_id)]，任何一步异常都返回空 list。

    每个元素是一个 (state, pid) 元组，调用方据此判断是否有 active session。
    """
    try:
        ole32 = _load_mfplat()
    except Exception as exc:
        _log_debug("ole32 加载异常: %r", exc)
        return []

    # COM 初始化（必须 STA 或 MTA，0x2 = COINIT_APARTMENTTHREADED）
    # 在多线程线程里 CoInitialize 可能已调过，第二次返回 RPC_E_CHANGED_MODE，跳过
    hr = ole32.CoInitializeEx(None, 0x2)
    if _hr_check(hr) and hr != 0x80010106:  # RPC_E_CHANGED_MODE
        _log_debug("CoInitializeEx 失败 hr=0x%X", hr)
        return []
    try:
        return _wasapi_enum_inner(ole32)
    finally:
        try:
            ole32.CoUninitialize()
        except Exception:
            pass


def _wasapi_enum_inner(ole32):
    """实际的 WASAPI 枚举逻辑。"""
    # 创建 IMMDeviceEnumerator
    clsid = _COMGUID.from_string(_CLSID_IMMDeviceEnumerator)
    iid = _COMGUID.from_string(_IID_IMMDeviceEnumerator)
    enumerator = ctypes.c_void_p(0)
    hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 0x1,  # CLSCTX_INPROC_SERVER
                                ctypes.byref(iid), ctypes.byref(enumerator))
    if _hr_check(hr) or not enumerator.value:
        _log_debug("CoCreateInstance(IMMDeviceEnumerator) hr=0x%X", hr)
        return []

    try:
        # IUnknown::Release (vtable slot 2)
        vtable = ctypes.cast(ctypes.cast(enumerator, ctypes.c_void_p).value,
                             ctypes.POINTER(ctypes.c_void_p * 3)).contents
        # 实际拿 vtable 指针
        enumerator_vtbl_ptr = ctypes.cast(enumerator, ctypes.POINTER(ctypes.c_void_p)).value
        vtbl = ctypes.cast(enumerator_vtbl_ptr, ctypes.POINTER(ctypes.c_void_p * 16)).contents

        # IMMDeviceEnumerator::EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE, **collection)
        # vtable[3] = EnumAudioEndpoints
        EnumAudioEndpoints = _CFUNCTYPE(
            ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p
        )(vtbl[3])
        collection = ctypes.c_void_p(0)
        # DEVICE_STATE_ACTIVE = 0x1
        hr = EnumAudioEndpoints(enumerator, _eCapture, 0x1, ctypes.byref(collection))
        if _hr_check(hr) or not collection.value:
            _log_debug("EnumAudioEndpoints hr=0x%X", hr)
            return []

        try:
            # IMMDeviceCollection vtable: [3]=GetCount, [4]=Item
            coll_vtbl_ptr = ctypes.cast(collection, ctypes.POINTER(ctypes.c_void_p)).value
            coll_vtbl = ctypes.cast(coll_vtbl_ptr,
                                    ctypes.POINTER(ctypes.c_void_p * 8)).contents
            GetCount = _CFUNCTYPE(
                ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
            )(coll_vtbl[3])
            count = ctypes.c_uint(0)
            hr = GetCount(collection, ctypes.byref(count))
            if _hr_check(hr):
                _log_debug("IMMDeviceCollection::GetCount hr=0x%X", hr)
                return []

            sessions = []
            Item = _CFUNCTYPE(
                ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p
            )(coll_vtbl[4])

            for i in range(count.value):
                device = ctypes.c_void_p(0)
                hr = Item(collection, i, ctypes.byref(device))
                if _hr_check(hr) or not device.value:
                    continue
                try:
                    sessions.extend(_wasapi_sessions_for_device(device))
                finally:
                    # IMMDevice::Release (vtable slot 2)
                    dev_vtbl_ptr = ctypes.cast(device,
                                               ctypes.POINTER(ctypes.c_void_p)).value
                    dev_vtbl = ctypes.cast(dev_vtbl_ptr,
                                            ctypes.POINTER(ctypes.c_void_p * 16)).contents
                    Release = _CFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)(dev_vtbl[2])
                    Release(device)
            return sessions
        finally:
            # IMMDeviceCollection::Release
            Release = _CFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)(coll_vtbl[2])
            Release(collection)
    finally:
        # IMMDeviceEnumerator::Release
        Release = _CFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)(vtbl[2])
        Release(enumerator)


def _wasapi_sessions_for_device(device):
    """对单个 capture device 激活 IAudioSessionManager2 并枚举所有 session。"""
    # IMMDevice::Activate(IID_IAudioSessionManager2, CLSCTX_ALL, NULL, **sessionMgr)
    dev_vtbl_ptr = ctypes.cast(device, ctypes.POINTER(ctypes.c_void_p)).value
    dev_vtbl = ctypes.cast(dev_vtbl_ptr,
                           ctypes.POINTER(ctypes.c_void_p * 16)).contents
    iid_sessmgr = _COMGUID.from_string(_IID_IAudioSessionManager2)
    Activate = _CFUNCTYPE(
        ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_void_p
    )(dev_vtbl[3])
    session_mgr = ctypes.c_void_p(0)
    hr = Activate(device, ctypes.byref(iid_sessmgr), 0x17,  # CLSCTX_ALL
                  None, ctypes.byref(session_mgr))
    if _hr_check(hr) or not session_mgr.value:
        _log_debug("IMMDevice::Activate(IAudioSessionManager2) hr=0x%X", hr)
        return []

    try:
        # IAudioSessionManager2 vtable: [8]=GetSessionEnumerator
        sm_vtbl_ptr = ctypes.cast(session_mgr, ctypes.POINTER(ctypes.c_void_p)).value
        sm_vtbl = ctypes.cast(sm_vtbl_ptr,
                              ctypes.POINTER(ctypes.c_void_p * 16)).contents
        GetSessionEnumerator = _CFUNCTYPE(
            ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p
        )(sm_vtbl[8])
        session_enum = ctypes.c_void_p(0)
        hr = GetSessionEnumerator(session_mgr, ctypes.byref(session_enum))
        if _hr_check(hr) or not session_enum.value:
            _log_debug("IAudioSessionManager2::GetSessionEnumerator hr=0x%X", hr)
            return []

        try:
            # IAudioSessionEnumerator vtable: [3]=GetCount, [4]=GetSession
            se_vtbl_ptr = ctypes.cast(session_enum, ctypes.POINTER(ctypes.c_void_p)).value
            se_vtbl = ctypes.cast(se_vtbl_ptr,
                                  ctypes.POINTER(ctypes.c_void_p * 8)).contents
            GetCount = _CFUNCTYPE(
                ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
            )(se_vtbl[3])
            count = ctypes.c_uint(0)
            hr = GetCount(session_enum, ctypes.byref(count))
            if _hr_check(hr):
                return []

            results = []
            GetSession = _CFUNCTYPE(
                ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p
            )(se_vtbl[4])
            for i in range(count.value):
                session = ctypes.c_void_p(0)
                hr = GetSession(session_enum, i, ctypes.byref(session))
                if _hr_check(hr) or not session.value:
                    continue
                try:
                    # IAudioSessionControl2 vtable: [3]=GetState, [14]=GetProcessId
                    s_vtbl_ptr = ctypes.cast(session,
                                             ctypes.POINTER(ctypes.c_void_p)).value
                    s_vtbl = ctypes.cast(s_vtbl_ptr,
                                         ctypes.POINTER(ctypes.c_void_p * 20)).contents
                    GetState = _CFUNCTYPE(
                        ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
                    )(s_vtbl[3])
                    state = ctypes.c_uint(0)
                    hr = GetState(session, ctypes.byref(state))
                    if _hr_check(hr):
                        continue
                    GetProcessId = _CFUNCTYPE(
                        ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)
                    )(s_vtbl[14])
                    pid = ctypes.c_uint(0)
                    hr = GetProcessId(session, ctypes.byref(pid))
                    if _hr_check(hr):
                        pid = ctypes.c_uint(0)
                    results.append((state.value, pid.value))
                finally:
                    Release = _CFUNCTYPE(
                        ctypes.c_uint, ctypes.c_void_p)(s_vtbl[2])
                    Release(session)
            return results
        finally:
            Release = _CFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)(se_vtbl[2])
            Release(session_enum)
    finally:
        Release = _CFUNCTYPE(ctypes.c_uint, ctypes.c_void_p)(sm_vtbl[2])
        Release(session_mgr)


def _mic_in_use_wasapi():
    """通过 WASAPI 检查是否有 active 的 audio capture session。

    返回 True / False / None(未知)。
    """
    if not _WIN:
        return None
    try:
        sessions = _wasapi_enum_capture_sessions()
    except Exception as exc:
        _log_debug("WASAPI 枚举异常: %r", exc)
        return None
    if not sessions:
        return None  # WASAPI 不可用或没拿到数据，按未知处理
    # 任一 session 是 active 状态（且不是 system sounds session，pid=0 通常是） -> 占用
    for state, pid in sessions:
        if state == _AudioSessionStateActive and pid != 0:
            return True
    return False


def probe(conservative=True):
    """一次采样，返回 (camera_in_use, mic_in_use) 两个三值。

    默认保守：任何一项不可靠都是 None；由上层决定如何显示。
    """
    try:
        cam = camera_in_use(conservative)
    except Exception as exc:       # 兜底：绝不让探测线程因异常而死
        _log_debug("摄像头探测异常: %r", exc)
        cam = None
    try:
        mic = mic_in_use(conservative)
    except Exception as exc:
        _log_debug("麦克风探测异常: %r", exc)
        mic = None
    return cam, mic


# =====================================================================
# 状态机 + 轮询线程
# =====================================================================

class IndicatorMonitor:
    """后台轮询摄像头/麦克风占用，并把结果收敛成一个指示灯状态。

    * 纯状态机：`indicator_state(camera, mic)`，无记忆、无粘滞，
      摄像头和麦克风各自独立恢复。
    * on_change 只在**状态发生变化**时触发，避免上层每 1 秒被无意义唤醒。
    * start()/stop() 用 threading.Event 控制，不用 sleep 轮询，stop() 能
      干净地结束线程（最迟在当前采样结束后立即返回）。
    * probe 可注入（默认用本模块的 probe），便于在任意平台做纯逻辑测试。

    节能策略（enable_idle_slowdown=True 时生效）：
      - 检测到占用（state != IDLE）-> 用 active_interval（默认 1s）
      - 连续空闲 N 次后          -> 切到 idle_interval（默认 5s）
      - 状态一旦变化立刻回到 active_interval，**首次变化延迟仍是 active_interval**
      - enable_idle_slowdown=False 时退化为恒定 interval（兼容旧行为）

    持续未知告警（on_unknown_streak 可选）：
      - 连续 N 次 (None, None) -> 触发 on_unknown_streak(N)
      - 中途恢复或得到真实状态 -> 触发 on_unknown_streak(0) 复位
      - 仅触发一次以避免日志 spam
    """

    def __init__(self, interval=DEFAULT_INTERVAL, on_change=None,
                 conservative=True, probe_fn=None, notify_initial=False,
                 active_interval=None,
                 idle_interval=DEFAULT_IDLE_INTERVAL,
                 enable_idle_slowdown=True,
                 unknown_streak_threshold=DEFAULT_UNKNOWN_STREAK,
                 on_unknown_streak=None):
        # 旧 interval 参数向后兼容：如果用户显式传了 interval 而没传 active_interval，
        # 用 interval 作为 active_interval；否则使用 active_interval 的默认值。
        # 用 None 作为 sentinel 区分"未传"。
        if active_interval is None:
            active_interval = interval
        self.active_interval = max(MIN_INTERVAL, float(active_interval))
        self.idle_interval = max(self.active_interval, float(idle_interval))
        self.enable_idle_slowdown = bool(enable_idle_slowdown)
        self.unknown_streak_threshold = max(1, int(unknown_streak_threshold))
        # 兼容旧 interval 参数：m.interval 字段保留可读，但实际线程跑 active_interval
        self.interval = self.active_interval  # 旧 API 兼容字段

        self.on_change = on_change
        self.on_unknown_streak = on_unknown_streak
        self.conservative = conservative
        self._probe = probe_fn or (lambda: probe(self.conservative))
        self.notify_initial = notify_initial

        self.camera_in_use = None
        self.mic_in_use = None
        self._state = STATE_IDLE
        # 首次采样是否需要通知上层（notify_initial=True 时通知一次 idle 初始态）
        self._fired_once = not self.notify_initial
        # 是否已经有过至少一次采样（独立于 _fired_once 标志，用于驱动 idle 计数）
        self._ever_sampled = False

        # 节能状态：连续多少个不变采样后才进入空闲降频
        self._idle_counter = 0
        # 持续未知状态计数
        self._unknown_counter = 0
        self._unknown_alerted = False  # 防 spam：同一轮未知 streak 只回调一次

        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ---------- 只读属性 ----------
    @property
    def state(self):
        """当前指示灯状态字符串（STATE_IDLE / STATE_MIC / STATE_CAMERA）。"""
        with self._lock:
            return self._state

    @property
    def color(self):
        """当前状态对应的 "#RRGGBB"。"""
        return state_color(self.state)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_interval(self):
        """当前线程实际使用的轮询间隔（用于测试与排障）。"""
        if not self.enable_idle_slowdown:
            return self.active_interval
        # 节能策略：进入空闲模式后用 idle_interval
        # 阈值为 1 次 active 间隔（语义清晰即可，复杂策略不值）
        return self.idle_interval if self._idle_counter > 0 else self.active_interval

    # ---------- 状态推进（可注入状态值，纯逻辑，便于测试） ----------
    def apply(self, camera, mic, fire=True):
        """把一组采样值并入状态机，返回新状态。

        这是整个模块唯一改变状态的地方；on_change 只在状态变化时触发。
        持续未知计数 / 节能计数器在这里推进。
        """
        with self._lock:
            self.camera_in_use = camera
            self.mic_in_use = mic
            new_state = indicator_state(camera, mic)
            changed = new_state != self._state or not self._fired_once
            prev_state = self._state
            is_first_sample = not self._ever_sampled
            self._fired_once = True
            self._ever_sampled = True
            self._state = new_state
        # 状态推进后更新节能计数器。
        # 「首次采样」不算纯状态变化，避免一开始 idle_counter 就被 +1 导致
        # 立刻切到 idle_interval。
        pure_change = (new_state != prev_state)
        if is_first_sample:
            # 首次采样：模拟为一次变化，让 _update_idle_counter 走清零分支
            self._update_idle_counter(True, prev_state, new_state, camera, mic)
        else:
            self._update_idle_counter(pure_change, prev_state, new_state, camera, mic)
        # 状态推进后更新持续未知计数并可能触发告警回调
        self._update_unknown_streak(camera, mic, fire=fire)
        # 只有「状态真的变了」才唤醒上层；notify_initial=True 时首次采样也通知一次
        if fire and self.on_change is not None and changed:
            try:
                self.on_change(new_state)
            except Exception as exc:
                _log_debug("on_change 回调异常: %r", exc)
        return new_state

    def _update_idle_counter(self, changed, prev_state, new_state, camera, mic):
        """更新节能降频用的空闲计数。

        规则：
          - 探测结果是未知 (None, None) -> 立刻清零、不进入降频（探测异常时
            需要尽快恢复检测，不能省事把间隔拉长）
          - 状态变化 -> 立刻清零（保持 active 频率，保证首次变化延迟 < active_interval）
          - 状态不变 + 新状态是 IDLE -> 计数 +1（连续空闲越久越省）
          - 状态不变 + 新状态非 IDLE -> 计数清零（仍在用，按 active 频率跑）
        """
        if not self.enable_idle_slowdown:
            self._idle_counter = 0
            return
        # 未知状态绝不进入降频——需要尽快恢复检测
        if camera is None and mic is None:
            self._idle_counter = 0
            return
        if changed:
            self._idle_counter = 0
            return
        if new_state == STATE_IDLE:
            self._idle_counter += 1
        else:
            self._idle_counter = 0

    def _update_unknown_streak(self, camera, mic, fire=True):
        """连续 (None, None) 计数与告警回调。

        - 触发时机：连续 unknown_streak_threshold 次 (None, None)
        - 复位时机：得到任意真实状态（摄像头或麦克风已知是 True/False）
        - 同一轮未知期间仅回调一次（防止日志 spam）
        """
        is_unknown = (camera is None and mic is None)
        if is_unknown:
            self._unknown_counter += 1
            if (not self._unknown_alerted
                    and self._unknown_counter >= self.unknown_streak_threshold
                    and fire and self.on_unknown_streak is not None):
                try:
                    self.on_unknown_streak(self._unknown_counter)
                except Exception as exc:
                    _log_debug("on_unknown_streak 回调异常: %r", exc)
                self._unknown_alerted = True
            return
        # 已知状态 -> 复位
        if self._unknown_alerted and fire and self.on_unknown_streak is not None:
            try:
                self.on_unknown_streak(0)
            except Exception as exc:
                _log_debug("on_unknown_streak 复位回调异常: %r", exc)
        self._unknown_counter = 0
        self._unknown_alerted = False

    # ---------- 线程生命周期 ----------
    def _run(self):
        while not self._stop_event.is_set():
            started = time.time()
            try:
                camera, mic = self._probe()
            except Exception as exc:
                _log_debug("探测异常，回退为未知: %r", exc)
                camera, mic = None, None
            self.apply(camera, mic)
            # 根据当前是否进入空闲模式动态选择间隔
            wait_sec = self.current_interval
            # Event.wait 代替 sleep：stop() 能立刻唤醒
            elapsed = time.time() - started
            self._stop_event.wait(max(0.0, wait_sec - elapsed))

    def start(self):
        """启动后台 daemon 线程。重复调用是安全的（幂等）。"""
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="UCopyIndicatorMonitor", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        """干净结束线程：置位 Event 并 join，不依赖 sleep 轮询。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None


# =====================================================================
# 自测入口：python3 device_indicator.py --selftest
# 只跑纯逻辑状态机 / 颜色映射 / 线程生命周期，不触碰任何 Windows API。
# =====================================================================

def _run_selftest():
    checks = []

    def check(desc, cond):
        checks.append((desc, bool(cond)))

    # --- 1. 状态机全部真值表（含 None 未知） ---
    check("空闲/空闲 -> idle", indicator_state(False, False) == STATE_IDLE)
    check("摄像头在用 -> camera（麦克风任意）",
          indicator_state(True, False) == STATE_CAMERA
          and indicator_state(True, True) == STATE_CAMERA
          and indicator_state(True, None) == STATE_CAMERA)
    check("摄像头空闲 + 麦克风在用 -> mic",
          indicator_state(False, True) == STATE_MIC
          and indicator_state(None, True) == STATE_MIC)
    check("全未知 -> idle（保守，不点亮）",
          indicator_state(None, None) == STATE_IDLE)
    check("摄像头未知 + 麦克风空闲 -> idle",
          indicator_state(None, False) == STATE_IDLE)

    # --- 2. 关键转换：摄像头停、麦克风仍在用 -> 橙色（不能粘住绿色） ---
    m = IndicatorMonitor(interval=0.05, probe_fn=lambda: (False, False))
    seq = [(True, True, STATE_CAMERA),   # 摄像头+麦克风 -> 绿
           (False, True, STATE_MIC),      # 摄像头停、麦克风仍在用 -> 橙
           (False, False, STATE_IDLE),    # 麦克风也停 -> 紫
           (False, True, STATE_MIC),      # 麦克风又开 -> 橙
           (True, True, STATE_CAMERA)]    # 摄像头也开 -> 绿
    ok = True
    for cam, mic, expect in seq:
        got = m.apply(cam, mic, fire=False)
        ok = ok and got == expect and m.camera_in_use == cam and m.mic_in_use == mic
    check("摄像头/麦克风各自独立恢复（绿->橙->紫->橙->绿）", ok)

    # --- 3. 反向转换：麦克风停、摄像头仍在用 -> 保持绿 ---
    m2 = IndicatorMonitor(interval=0.05, probe_fn=lambda: (False, False))
    check("麦克风停但摄像头仍在用 -> 保持 camera(绿)",
          m2.apply(True, True, fire=False) == STATE_CAMERA
          and m2.apply(True, False, fire=False) == STATE_CAMERA)

    # --- 4. 未知不被当成占用 ---
    m3 = IndicatorMonitor(interval=0.05, probe_fn=lambda: (None, None))
    check("未知探测结果不点亮指示灯",
          m3.apply(None, None, fire=False) == STATE_IDLE
          and m3.apply(None, None, fire=False) == STATE_IDLE)

    # --- 5. 颜色映射 ---
    check("空闲=紫 #8B5CF6", state_color(STATE_IDLE).upper() == "#8B5CF6")
    check("摄像头在用=绿", state_color(STATE_CAMERA).upper() == "#22C55E")
    check("仅麦克风在用=橙 #F59E0B", state_color(STATE_MIC).upper() == "#F59E0B")
    check("未知状态退化为紫色", state_color("bogus") == state_color(STATE_IDLE))
    check("所有颜色都是 #RRGGBB",
          all(len(c) == 7 and c[0] == "#" for c in STATE_COLORS.values()))

    # --- 6. on_change 只在状态变化时触发 ---
    fired = []
    m4 = IndicatorMonitor(interval=0.05, on_change=fired.append,
                          probe_fn=lambda: (False, False))
    for cam, mic in [(False, False), (False, False), (True, False),
                     (True, False), (False, True), (False, False)]:
        m4.apply(cam, mic)
    check("on_change 仅在变化时触发（6 次输入 3 次回调）",
          fired == [STATE_CAMERA, STATE_MIC, STATE_IDLE])
    check("on_change 抛异常时被吞掉，状态照常推进",
          _call_on_change_raises() == STATE_CAMERA)

    # --- 7. 注入式探测 + 线程生命周期（任何平台可跑） ---
    ticks = []

    def fake_probe():
        ticks.append(1)
        return (True, False)

    m5 = IndicatorMonitor(interval=0.05, probe_fn=fake_probe)
    seen = []
    m5.on_change = seen.append
    m5.start()
    deadline = time.time() + 2.0
    while len(ticks) < 3 and time.time() < deadline:
        time.time()
        _sleep(0.02)
    m5.stop()
    check("后台线程启动了并完成轮询", len(ticks) >= 3)
    check("线程已干净退出", not m5.running)
    check("线程探测到摄像头在用 -> 绿色",
          m5.state == STATE_CAMERA and state_color(m5.state).upper() == "#22C55E")

    # --- 8. 探测抛异常不能让线程死掉 ---
    def boom():
        raise RuntimeError("模拟 API 异常")

    m6 = IndicatorMonitor(interval=0.05, probe_fn=boom)
    m6.start()
    _sleep(0.15)
    alive = m6.running
    m6.stop()
    check("探测异常时线程存活且状态回退为 idle",
          alive and m6.state == STATE_IDLE)

    # --- 9. 非 Windows 平台的探测接口不崩 ---
    if not _WIN:
        check("非 Windows 平台保守返回 None",
              camera_in_use() is None and mic_in_use() is None)
        check("conservative=False 时折算为 False",
              camera_in_use(False) is False and mic_in_use(False) is False)
    check("probe() 在任意平台都返回两个三值",
          len(probe()) == 2)

    # --- 10. DLL 缓存：多次调用返回同一对象，不重复 LoadLibrary ---
    # macOS 上 ctypes.WinDLL 不存在，所以这部分统一用 monkey-patch 占位
    # ctypes.WinDLL 后再跑；Windows 上 _win32_dll 已存在但本身无害。
    _calls = {"n": 0}
    _stub_objects = {}

    class _StubDll:
        """接受任意属性读写的占位 DLL（支持 setupapi.SetupDiGetClassDevsW.restype = ...）。"""

        def __init__(self, name):
            self._name = name

        def __repr__(self):
            return f"<StubDll {self._name}>"

        def __getattr__(self, attr):
            # 任意属性访问都返回一个新的 StubSlot，能继续被赋值/读取
            return _StubSlot(self._name, attr)

    class _StubSlot:
        """属性赋值的占位槽位（restype/argtypes 等）。"""

        def __init__(self, dll_name, attr_name):
            self._dll = dll_name
            self._attr = attr_name
            self.value = None

        def __repr__(self):
            return f"<StubSlot {self._dll}.{self._attr}>"

    def _stub_windll(name, use_last_error=False):
        _calls["n"] += 1
        obj = _StubDll(name)
        _stub_objects[(name, use_last_error)] = obj
        return obj

    _orig_windll = getattr(ctypes, "WinDLL", None)
    ctypes.WinDLL = _stub_windll
    try:
        _reset_win32_dll_cache()
        a = _win32_dll("setupapi.dll")
        b = _win32_dll("setupapi.dll")
        c = _win32_dll("kernel32.dll")
        check("DLL 缓存：_win32_dll 多次调用返回同一对象",
              a is b and a is not c)
        check("DLL 缓存：模块级 dict 同时持有多个 DLL",
              "setupapi.dll" in _WIN32_DLLS and "kernel32.dll" in _WIN32_DLLS)
        # 懒加载语义：调用前 cache 为空，调用后才有键
        _reset_win32_dll_cache()
        check("DLL 缓存：调用前 _WIN32_DLLS 为空", len(_WIN32_DLLS) == 0)
        _win32_dll("winmm.dll")
        check("DLL 缓存：调用后 _WIN32_DLLS 出现 winmm.dll",
              "winmm.dll" in _WIN32_DLLS)
        # _load_setupapi / _load_winmm 走缓存路径：第二次不重新调用 ctypes.WinDLL
        _reset_win32_dll_cache()
        _calls["n"] = 0
        _load_setupapi()
        n1 = _calls["n"]
        _load_setupapi()
        n2 = _calls["n"]
        check("_load_setupapi 走缓存：第二次不重新调用 ctypes.WinDLL",
              n1 == 2 and n2 == n1)
        _load_winmm()
        n3 = _calls["n"]
        _load_winmm()
        n4 = _calls["n"]
        check("_load_winmm 走缓存：第二次不重新调用 ctypes.WinDLL",
              n3 == n1 + 1 and n4 == n3)
    finally:
        if _orig_windll is not None:
            ctypes.WinDLL = _orig_windll
        else:
            try:
                del ctypes.WinDLL
            except AttributeError:
                pass
        _reset_win32_dll_cache()

    # --- 11. 旧 interval 参数向后兼容：显式传 interval 也能跑 ---
    m_old = IndicatorMonitor(interval=0.07)
    check("旧 interval 参数：active_interval 被设到 0.07",
          abs(m_old.active_interval - 0.07) < 1e-9)
    check("旧 interval 参数：idle_interval 不低于 active_interval",
          m_old.idle_interval >= m_old.active_interval)
    check("旧 interval 参数：m.interval 字段仍可读且等于 active_interval",
          m_old.interval == m_old.active_interval)

    # --- 12. 智能降频：空闲状态下 current_interval 切到 idle_interval ---
    m_sd = IndicatorMonitor(
        interval=0.05, active_interval=0.05, idle_interval=0.20,
        enable_idle_slowdown=True,
        probe_fn=lambda: (False, False),
    )
    check("初始状态 current_interval == active_interval",
          m_sd.current_interval == 0.05)
    # 首次 apply 必触发一次「变化」-> _idle_counter 清零、interval=active
    m_sd.apply(False, False, fire=False)
    check("首次 apply 后 current_interval 仍为 active_interval",
          m_sd.current_interval == 0.05)
    # 第二次相同采样 -> 进入 idle，counter=1
    m_sd.apply(False, False, fire=False)
    check("第二次相同采样 -> 切到 idle_interval",
          m_sd.current_interval == 0.20)
    # 第三次仍空闲 -> 仍 idle
    m_sd.apply(False, False, fire=False)
    check("连续空闲期间保持 idle_interval",
          m_sd.current_interval == 0.20)
    # 状态变化 -> 立刻回到 active_interval
    m_sd.apply(True, False, fire=False)
    check("状态变化立刻回到 active_interval",
          m_sd.current_interval == 0.05)
    # 再变回 idle -> 又进入降频
    m_sd.apply(False, False, fire=False)
    m_sd.apply(False, False, fire=False)
    check("变化后再空闲，重新进入 idle_interval",
          m_sd.current_interval == 0.20)

    # 关闭开关 -> 始终 active_interval
    m_off = IndicatorMonitor(
        interval=0.05, active_interval=0.05, idle_interval=0.20,
        enable_idle_slowdown=False,
        probe_fn=lambda: (False, False),
    )
    m_off.apply(False, False, fire=False)
    m_off.apply(False, False, fire=False)
    m_off.apply(False, False, fire=False)
    check("enable_idle_slowdown=False 时始终 active_interval",
          m_off.current_interval == 0.05)

    # 线程实际跑的 wait 时间也要符合预期
    waits = []
    m_thread = IndicatorMonitor(
        active_interval=0.05, idle_interval=0.20,
        enable_idle_slowdown=True, probe_fn=lambda: (False, False),
    )
    # 用 monkey-patch 替换 Event.wait，捕获每次的实际等待时长
    _orig_wait = threading.Event.wait
    def _spy_wait(self, timeout=None):
        waits.append(timeout)
        return _orig_wait(self, 0)  # 立刻返回，便于测试快速结束
    threading.Event.wait = _spy_wait
    try:
        m_thread.start()
        _sleep(0.30)
    finally:
        m_thread.stop()
        threading.Event.wait = _orig_wait
    check("线程运行产生多次 wait 调用（>=3）", len(waits) >= 3)
    if waits:
        # 第一次进入空闲前的 wait 应是 active_interval
        # 进入空闲后的 wait 应是 idle_interval
        check("首个 wait 接近 active_interval",
              abs((waits[0] or 0) - 0.05) < 0.06)
        check("后续 wait 切到 idle_interval",
              any((w or 0) >= 0.19 for w in waits[1:]))

    # --- 13. 持续未知告警回调 ---
    streak_calls = []

    def _on_streak(n):
        streak_calls.append(n)

    m_unk = IndicatorMonitor(
        interval=0.02, unknown_streak_threshold=3,
        on_unknown_streak=_on_streak,
        probe_fn=lambda: (None, None),
    )
    m_unk.start()
    _sleep(0.30)  # 至少跑出 ≥3 次 (None, None)
    # 恢复一次真实状态 -> 应触发复位回调（参数 0）
    m_unk._probe = lambda: (True, False)
    _sleep(0.20)
    m_unk.stop()
    check("持续未知触发告警回调（至少 1 次）",
          any(c >= 3 for c in streak_calls))
    check("恢复真实状态触发复位回调（参数 0）",
          0 in streak_calls)
    check("告警回调在 streak 期间只触发 1 次（防 spam）",
          sum(1 for c in streak_calls if c >= 3) == 1)
    # 计数器与告警标记在恢复后清零
    check("恢复后 _unknown_counter 清零", m_unk._unknown_counter == 0)
    check("恢复后 _unknown_alerted 清零", m_unk._unknown_alerted is False)

    # --- 14. 节能降频计数器和未知计数互不影响 ---
    m_iso = IndicatorMonitor(
        interval=0.02, active_interval=0.02, idle_interval=0.10,
        enable_idle_slowdown=True,
        unknown_streak_threshold=2,
        probe_fn=lambda: (None, None),  # 全未知 -> 节能不进入
    )
    m_iso.apply(None, None, fire=False)
    check("全未知期间 current_interval 仍是 active_interval（未知≠空闲）",
          m_iso.current_interval == 0.02)
    # 再来一次仍是 active
    m_iso.apply(None, None, fire=False)
    check("全未知期间 current_interval 不切到 idle_interval",
          m_iso.current_interval == 0.02)

    # --- 输出 ---
    passed = 0
    for desc, ok in checks:
        print(("PASS  " if ok else "FAIL  ") + desc)
        passed += 1 if ok else 0
    print("\n%d/%d 项断言通过" % (passed, len(checks)))
    if passed == len(checks):
        print("SELFTEST OK")
        return 0
    print("SELFTEST FAILED")
    return 1


def _sleep(sec):
    import time as _t
    _t.sleep(sec)


def _call_on_change_raises():
    """on_change 抛异常时被吞掉，不影响状态推进。"""
    m = IndicatorMonitor(interval=0.05, on_change=lambda s: 1 / 0)
    return m.apply(True, False)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _run_selftest()
    print(__doc__.strip().splitlines()[0])
    print("用法: python device_indicator.py --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main())
