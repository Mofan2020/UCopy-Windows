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

# ---------- Windows 常量（模块级只是整数，不触发任何 API 调用） ----------
_WIN = sys.platform == "win32"

_DEVCLASS_CAMERA = "{ca3e7ab9-b4c3-4ae6-8251-58ef5fbb7f7c}"
_DEVINTERFACE_VIDEO_DEVICE = "{e6cafbd7-1866-4b66-9f8d-431d5f55c1da}"

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

# 「占用类」错误白名单：只有命中这些才判定为占用
CAMERA_IN_USE_ERRORS = frozenset((ERROR_SHARING_VIOLATION, ERROR_BUSY, 100, 1117))

MMSYSERR_NOERROR = 0
MMSYSERR_ALLOCATED = 10
WAVERR_STILLPLAYING = 33
MIC_IN_USE_CODES = frozenset((MMSYSERR_ALLOCATED, WAVERR_STILLPLAYING))

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_QUERY = 0x80000000
CALLBACK_NULL = 0

DEFAULT_INTERVAL = 1.0


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
    """懒加载 setupapi.dll（import 本模块时不会走到这里）。"""
    setupapi = ctypes.WinDLL("setupapi.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

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
    winmm = ctypes.WinDLL("winmm")
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


def _enumerate_camera_paths():
    """枚举所有**当前连接**的摄像头设备接口路径（虚拟驱动同样在此列）。

    返回 list[str]；任何一步失败都抛给调用方，由上层转成 None（未知）。
    """
    setupapi, _kernel32 = _load_setupapi()
    class_guid = _guid(_DEVCLASS_CAMERA)
    iface_guid = _guid(_DEVINTERFACE_VIDEO_DEVICE)
    paths = []

    dev_info = setupapi.SetupDiGetClassDevsW(ctypes.byref(class_guid), None, None,
                                            DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not dev_info or dev_info == INVALID_HANDLE_VALUE:
        raise OSError("SetupDiGetClassDevsW 失败, err=%d" % ctypes.get_last_error())
    try:
        # --- 1) 设备接口枚举：拿到可直接 CreateFile 的 \\?\usb#... 路径 ---
        index = 0
        while True:
            ifdata = _SP_DEVICE_INTERFACE_DATA()
            ifdata.cbSize = ctypes.sizeof(ifdata)
            if not setupapi.SetupDiEnumDeviceInterfaces(dev_info, None,
                                                       ctypes.byref(iface_guid),
                                                       index, ctypes.byref(ifdata)):
                break  # 没有更多设备了
            path = _interface_path(setupapi, dev_info, ifdata)
            if path:
                paths.append(path)
            index += 1

        # --- 2) 设备实例枚举兜底：有些虚拟驱动只注册了类而没暴露标准接口，
        #        这时至少记下它的实例 ID，说明该类里确实存在设备。 ---
        if not paths:
            index = 0
            while True:
                devdata = _SP_DEVINFO_DATA()
                devdata.cbSize = ctypes.sizeof(devdata)
                if not setupapi.SetupDiEnumDeviceInfo(dev_info, index,
                                                      ctypes.byref(devdata)):
                    break
                buf = ctypes.create_unicode_buffer(512)
                if setupapi.SetupDiGetDeviceInstanceIdW(dev_info, ctypes.byref(devdata),
                                                       buf, 512, None):
                    _log_debug("摄像头设备实例(无接口路径, 忽略): %s", buf.value)
                index += 1
    finally:
        # 枚举句柄必须释放，否则每次轮询都会泄漏一个设备信息集
        try:
            setupapi.SetupDiDestroyDeviceInfoList(dev_info)
        except Exception:
            pass

    # 去重，保持顺序稳定
    seen = set()
    unique = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _open_device_exclusive(kernel32, path):
    """以独占方式(dwShareMode=0)打开设备路径。

    返回 (True,)  打开成功 -> 说明空闲
          (False, err) 打开失败，err 为 GetLastError
    """
    handle = kernel32.CreateFileW(path, GENERIC_READ, 0, None,
                                 OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle is None or handle == INVALID_HANDLE_VALUE or handle == 0:
        return False, ctypes.get_last_error()
    try:
        return True, 0
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
      opened      至少有一次打开成功 -> 该设备当前空闲
      in_use_err  某次打开返回了明确的占用类错误码（否则为 None）

    说明：摄像头应用通常以独占方式打开设备，因此「连共享模式都打不开」
    是很强的占用证据；但只有明确的占用类错误码才判定为占用，
    路径失效/权限不足一律不算，避免误亮灯。
    """
    candidates = [path]
    stripped = _strip_interface_suffix(path)
    if stripped:
        candidates.append(stripped)

    in_use_err = None
    opened = False
    for cand in candidates:
        # 1) 独占打开
        try:
            ok, err = _open_device_exclusive(kernel32, cand)
        except Exception as exc:
            _log_debug("打开摄像头设备异常 %s: %r", cand, exc)
            continue
        if ok:
            opened = True
            break
        if err in CAMERA_IN_USE_ERRORS:
            in_use_err = err
            break

        # 2) 独占打不开时，再试允许读共享的打开方式。
        #    若设备被别人独占持有，这里同样会返回共享冲突。
        try:
            handle = kernel32.CreateFileW(cand, GENERIC_READ,
                                          FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                          OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
        except Exception as exc:
            _log_debug("共享方式打开异常 %s: %r", cand, exc)
            continue
        if handle and handle != INVALID_HANDLE_VALUE and handle != 0:
            try:
                opened = True
                break
            finally:
                kernel32.CloseHandle(handle)
        else:
            err = ctypes.get_last_error()
            if err in CAMERA_IN_USE_ERRORS:
                in_use_err = err
                break
        _log_debug("摄像头设备 %s 打开失败(err=%d)，尝试下一种方式", cand, err)
    return opened, in_use_err


def camera_in_use(conservative=True):
    """摄像头是否正在被任意程序使用。

    返回 True / False / None(未知)。
    conservative=True（默认）时，任何探测不可靠（枚举失败、全部设备都打不开）
    返回 None；conservative=False 时把未知折算成 False（当作空闲）。
    """
    if not _WIN:
        # 非 Windows 平台没有可探测的摄像头类别
        return False if not conservative else None
    probed = 0
    try:
        paths = _enumerate_camera_paths()
        _setupapi, kernel32 = _load_setupapi()
    except Exception as exc:
        _log_debug("摄像头枚举异常: %r", exc)
        return False if not conservative else None

    if not paths:
        # 一个摄像头设备都没接：不可能有程序在占用 -> 空闲
        return False

    for path in paths:
        try:
            opened, in_use_err = _open_variants(kernel32, path)
        except Exception as exc:
            _log_debug("探测摄像头设备异常 %s: %r", path, exc)
            continue
        if in_use_err is not None:
            return True           # 明确的共享冲突/占用
        if opened:
            probed += 1           # 能打开 -> 空闲
        else:
            _log_debug("摄像头设备 %s 各种方式都打不开，忽略", path)

    return False if probed else (False if not conservative else None)


def mic_in_use(conservative=True):
    """麦克风是否正在被任意程序使用。

    返回 True / False / None(未知)。
    判定依据：对每个 waveIn 录音设备做一次 waveInOpen 尝试。
    """
    if not _WIN:
        return False if not conservative else None
    try:
        winmm = _load_winmm()
        count = winmm.waveInGetNumDevs()
    except Exception as exc:
        _log_debug("winmm 加载/枚举异常: %r", exc)
        return False if not conservative else None

    if not count:
        return False               # 没有录音设备 -> 不可能在用

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
            # 打开成功 -> 设备空闲，必须立刻关闭，绝不抢占
            try:
                winmm.waveInClose(handle)
            except Exception:
                pass
            handle = None
            continue
        # 打开失败：句柄理论上不会有值，保险起见也关一次
        if handle:
            try:
                winmm.waveInClose(handle)
            except Exception:
                pass
        if rc in MIC_IN_USE_CODES:
            return True
        _log_debug("录音设备 %d waveInOpen 返回 %d，忽略", dev_id, rc)

    return False if probed else (False if not conservative else None)


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
    """

    def __init__(self, interval=DEFAULT_INTERVAL, on_change=None,
                 conservative=True, probe_fn=None, notify_initial=False):
        self.interval = max(0.05, float(interval))
        self.on_change = on_change
        self.conservative = conservative
        self._probe = probe_fn or (lambda: probe(self.conservative))
        self.notify_initial = notify_initial

        self.camera_in_use = None
        self.mic_in_use = None
        self._state = STATE_IDLE
        # 首次采样是否需要通知上层（notify_initial=True 时通知一次 idle 初始态）
        self._fired_once = not self.notify_initial

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

    # ---------- 状态推进（可注入状态值，纯逻辑，便于测试） ----------
    def apply(self, camera, mic, fire=True):
        """把一组采样值并入状态机，返回新状态。

        这是整个模块唯一改变状态的地方；on_change 只在状态变化时触发。
        """
        with self._lock:
            self.camera_in_use = camera
            self.mic_in_use = mic
            new_state = indicator_state(camera, mic)
            changed = new_state != self._state or not self._fired_once
            self._fired_once = True
            self._state = new_state
        # 只有「状态真的变了」才唤醒上层；notify_initial=True 时首次采样也通知一次
        if fire and self.on_change is not None and changed:
            try:
                self.on_change(new_state)
            except Exception as exc:
                _log_debug("on_change 回调异常: %r", exc)
        return new_state

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
            # Event.wait 代替 sleep：stop() 能立刻唤醒
            elapsed = time.time() - started
            self._stop_event.wait(max(0.0, self.interval - elapsed))

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
