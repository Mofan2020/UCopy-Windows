<#
UCopy 隐私检测探针 — Windows 真机排障用

诊断目标:
  1. 当前机器的摄像头设备到底有几个、各 GUID 能不能枚举到
  2. 对每个摄像头以不同 share_mode CreateFileW,看返回的 Win32 错误码
  3. waveIn + WASAPI 麦克风探测两条路径的输出

用法 (在 Windows 机器上,以管理员权限的 PowerShell 跑):
    cd <UCopy 仓库>
    powershell -ExecutionPolicy Bypass -File diagnostics/probe.ps1
    # 或带开关: ./diagnostics/probe.ps1 -QuietOnSuccess -OnlyCamera
    # -QuietOnSuccess: 成功打开就不打,只打错误码 — 输出更短,排查错码
    # -OnlyCamera / -OnlyMic: 只跑摄像头 / 只跑麦克风

不需要任何 Python 环境,不依赖 UCopy.exe,纯 PowerShell + .NET P/Invoke。
结果复制回来给我,我直接看错码就能定位。
#>

[CmdletBinding()]
param(
    [switch]$QuietOnSuccess,
    [switch]$OnlyCamera,
    [switch]$OnlyMic
)

$ErrorActionPreference = 'Continue'
$src = @"
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public static class Probe
{
    public const uint GENERIC_READ          = 0x80000000;
    public const uint FILE_SHARE_READ       = 0x00000001;
    public const uint FILE_SHARE_WRITE      = 0x00000002;
    public const uint OPEN_EXISTING         = 3;
    public const uint FILE_ATTRIBUTE_NORMAL = 0x00000080;
    public static readonly IntPtr INVALID_HANDLE_VALUE = new IntPtr(-1);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateFileW(
        string lpFileName, uint dwDesiredAccess, uint dwShareMode,
        IntPtr lpSecurityAttributes, uint dwCreationDisposition,
        uint dwFlagsAndAttributes, IntPtr hTemplateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr h);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr SetupDiGetClassDevsW(
        IntPtr pClassGuid, string Enumerator, IntPtr hwndParent, uint Flags);

    [DllImport("setupapi.dll", SetLastError = true)]
    static extern bool SetupDiDestroyDeviceInfoList(IntPtr DeviceInfoSet);

    [DllImport("setupapi.dll", SetLastError = true)]
    static extern bool SetupDiEnumDeviceInterfaces(
        IntPtr DeviceInfoSet, IntPtr DeviceInfoData,
        IntPtr InterfaceClassGuid, uint MemberIndex,
        ref SP_DEVICE_INTERFACE_DATA DeviceInterfaceData);

    [StructLayout(LayoutKind.Sequential)]
    public struct SP_DEVICE_INTERFACE_DATA {
        public uint cbSize;
        public Guid InterfaceClassGuid;
        public uint Flags;
        public IntPtr Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct SP_DEVINFO_DATA {
        public uint cbSize;
        public Guid ClassGuid;
        public uint DevInst;
        public IntPtr Reserved;
    }

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetupDiGetDeviceInterfaceDetailW(
        IntPtr DeviceInfoSet, ref SP_DEVICE_INTERFACE_DATA DeviceInterfaceData,
        IntPtr DeviceInterfaceDetailData, uint DeviceInterfaceDetailDataSize,
        out uint RequiredSize, IntPtr DeviceInfoData);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool SetupDiGetDeviceInstanceIdW(
        IntPtr DeviceInfoSet, ref SP_DEVINFO_DATA DeviceInfoData,
        StringBuilder DeviceInstanceId, uint DeviceInstanceIdSize,
        out uint RequiredSize);

    public static uint TryOpen(string path, uint shareMode) {
        var h = CreateFileW(path, GENERIC_READ, shareMode, IntPtr.Zero,
                            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, IntPtr.Zero);
        if (h == INVALID_HANDLE_VALUE || h == IntPtr.Zero) {
            return (uint)Marshal.GetLastWin32Error();
        }
        CloseHandle(h);
        return 0;
    }

    // 返回 (path, instanceId, errorPerShareMode) 列表
    public static List<object[]> EnumCameraPaths(Guid[] guids) {
        var rows = new List<object[]>();
        foreach (var g in guids) {
            var clsGuid = Marshal.AllocHGlobal(Marshal.SizeOf<Guid>());
            Marshal.StructureToPtr(g, clsGuid, false);
            try {
                var set = SetupDiGetClassDevsW(clsGuid, null, IntPtr.Zero, 0x00000012);
                if (set == INVALID_HANDLE_VALUE) continue;
                try {
                    uint idx = 0;
                    while (true) {
                        var ifd = new SP_DEVICE_INTERFACE_DATA();
                        ifd.cbSize = (uint)Marshal.SizeOf<SP_DEVICE_INTERFACE_DATA>();
                        if (!SetupDiEnumDeviceInterfaces(set, IntPtr.Zero, clsGuid, idx, ref ifd)) break;
                        uint need = 0;
                        SetupDiGetDeviceInterfaceDetailW(set, ref ifd, IntPtr.Zero, 0, out need, IntPtr.Zero);
                        if (need == 0) { idx++; continue; }
                        var buf = Marshal.AllocHGlobal((int)need);
                        try {
                            if (!SetupDiGetDeviceInterfaceDetailW(set, ref ifd, buf, need, out _, IntPtr.Zero)) {
                                idx++; continue;
                            }
                            var path = Marshal.PtrToStringUni(buf);
                            // share mode 0=独占, 1=R, 3=RW
                            var e0 = TryOpen(path, 0);
                            var e1 = TryOpen(path, 1);
                            var e2 = TryOpen(path, 3);
                            rows.Add(new object[] { g.ToString(), path, e0, e1, e2 });
                        } finally { Marshal.FreeHGlobal(buf); }
                        idx++;
                    }
                } finally { SetupDiDestroyDeviceInfoList(set); }
            } finally { Marshal.FreeHGlobal(clsGuid); }
        }
        return rows;
    }
}
"@

Add-Type -TypeDefinition $src -Language CSharp

$guids = @(
    [Guid]'ca3e7ab9-b4c3-4ae6-8251-58ef5fbb7f7c',  # DEVCLASS_CAMERA
    [Guid]'e6cafbd7-1866-4b66-9f8d-431d5f55c1da',  # DEVINTERFACE_VIDEO_DEVICE
    [Guid]'65e8773d-8f56-11d0-a3b9-00a0c9223196',  # KSCATEGORY_VIDEO_CAMERA
    [Guid]'6994ad05-93ef-11d0-a3ac-00a0c9223196',  # KSCATEGORY_CAPTURE
    [Guid]'6994ad04-93ef-11d0-a3ac-00a0c9223196'   # KSCATEGORY_VIDEO
)

if (-not $OnlyMic) {
    Write-Host "===== 摄像头设备枚举 (5 个 GUID × 3 种 share_mode CreateFileW) =====" -ForegroundColor Cyan
    Write-Host ("share: 独占=0  仅R=1  RW=3")
    Write-Host ("Win32 错误码: 0=成功  5=ACCESS_DENIED  32=SHARING_VIOLATION  170=BUSY")
    Write-Host ""
    $rows = [Probe]::EnumCameraPaths($guids)
    Write-Host ("GUID                                    Path                                                           独占  仅R  RW")
    foreach ($r in $rows) {
        $g = $r[0]; $p = $r[1]; $e0 = $r[2]; $e1 = $r[3]; $e2 = $r[4]
        $truncated = if ($p.Length -gt 60) { $p.Substring(0,57) + "..." } else { $p }
        $allZero = ($e0 -eq 0 -and $e1 -eq 0 -and $e2 -eq 0)
        if ($QuietOnSuccess -and $allZero) { continue }
        $line = ("{0,-40} {1,-63} {2,5} {3,5} {4,5}" -f $g, $truncated, $e0, $e1, $e2)
        $color = if ($e0 -eq 5 -or $e0 -eq 32 -or $e0 -eq 170) { "Yellow" }
                 elseif ($allZero) { "Green" }
                 else { "Gray" }
        Write-Host $line -ForegroundColor $color
    }
    Write-Host ""
    Write-Host ("共枚举到 {0} 个摄像头设备路径 (含重复)" -f $rows.Count)
    Write-Host ""
}

if (-not $OnlyCamera) {
    Write-Host "===== 麦克风探测 (waveIn + WASAPI) =====" -ForegroundColor Cyan

    # waveIn
    Add-Type @"
using System.Runtime.InteropServices;
public static class WaveIn {
    [DllImport("winmm.dll")]
    public static extern uint waveInGetNumDevs();
}
"@
    $n = [WaveIn]::waveInGetNumDevs()
    Write-Host ("waveIn 录音设备数: {0}" -f $n)

    if ($n -gt 0) {
        # 用 Python 的 WAVE_FORMAT_PCM 8kHz 8bit mono 试独占打开
        $waveSrc = @"
using System;
using System.Runtime.InteropServices;
public static class WaveIn2 {
    [StructLayout(LayoutKind.Sequential)]
    public struct WAVEINCAPS {
        public ushort wMid, wPid, vDriverVersion;
        [MarshalAs(UnmanagedType.LPWStr)] public string szPname;
        public uint dwFormats, wChannels, wReserved1, wReserved2;
    }
    [DllImport("winmm.dll")]
    public static extern uint waveInGetDevCaps(uint uDeviceID, ref WAVEINCAPS pwic, uint cbwic);
    [DllImport("winmm.dll")]
    public static extern uint waveInOpen(out IntPtr phwi, uint uDeviceID, IntPtr pwfx, uint dwCallback, uint dwInstance, uint fdwOpen);
    [DllImport("winmm.dll")]
    public static extern uint waveInClose(IntPtr hwi);
    [StructLayout(LayoutKind.Sequential)]
    public struct WAVEFORMATEX {
        public ushort wFormatTag, nChannels;
        public uint nSamplesPerSec, nAvgBytesPerSec;
        public ushort nBlockAlign, wBitsPerSample, cbSize;
    }
    public static uint Try(uint i) {
        var fmt = new WAVEFORMATEX();
        fmt.wFormatTag = 1; fmt.nChannels = 1; fmt.nSamplesPerSec = 8000;
        fmt.nAvgBytesPerSec = 8000; fmt.nBlockAlign = 1; fmt.wBitsPerSample = 8; fmt.cbSize = 0;
        var pfmt = Marshal.AllocHGlobal(Marshal.SizeOf<WAVEFORMATEX>());
        Marshal.StructureToPtr(fmt, pfmt, false);
        IntPtr h = IntPtr.Zero;
        try {
            uint rc = waveInOpen(out h, i, pfmt, 0, 0, 0);
            if (rc == 0) { waveInClose(h); return 0; }
            return rc;
        } finally { Marshal.FreeHGlobal(pfmt); }
    }
    public static string Name(uint i) {
        var cap = new WAVEINCAPS();
        uint sz = (uint)Marshal.SizeOf<WAVEINCAPS>();
        waveInGetDevCaps(i, ref cap, sz);
        return cap.szPname ?? "<no name>";
    }
}
"@
        Add-Type -TypeDefinition $waveSrc -Language CSharp
        for ($i = 0; $i -lt [int]$n; $i++) {
            $name = [WaveIn2]::Name([uint32]$i)
            $rc = [WaveIn2]::Try([uint32]$i)
            $msg = if ($rc -eq 0) { "空闲(独占成功)" }
                   elseif ($rc -eq 10) { "占用(MMSYSERR_ALLOCATED)" }
                   elseif ($rc -eq 33) { "占用(WAVERR_STILLPLAYING)" }
                   else { "未知错误码 $rc" }
            $color = if ($rc -eq 0) { "Green" } elseif ($rc -eq 10 -or $rc -eq 33) { "Yellow" } else { "Gray" }
            Write-Host ("  [{0}] {1,-40} -> {2}" -f $i, $name, $msg) -ForegroundColor $color
        }
    }
    Write-Host ""

    # WASAPI — 用 PowerShell 直接调 COM 太繁,这里只跑一个 CountAudioSessions 探针看 API 是否可用
    Write-Host "WASAPI session 枚举 (via mmdevapi.dll 计数) ..." -ForegroundColor Cyan
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class WasapiProbe {
    public static uint DeviceCount() {
        // 这里只测 COM 初始化能不能成功 — 真正的 session 枚举需要 5 个 vtable 调用,
        // 用纯 .NET 调用太繁,探针仅确认 COM 链路通畅,完整枚举交给 device_indicator._wasapi_enum_capture_sessions
        Guid clsid = new Guid("A95664D2-9614-4F35-A746-DE8DB636B7BF");
        Guid iid   = new Guid("BCDE0395-E52F-467C-8E3D-C4579291692E");
        Type t = Type.GetTypeFromCLSID(clsid);
        try {
            object o = Activator.CreateInstance(t);
            if (o == null) return 99;
            Marshal.ReleaseComObject(o);
            return 0;
        } catch (Exception e) { return 1; }
    }
}
"@
    $rc = [WasapiProbe]::DeviceCount()
    if ($rc -eq 0) {
        Write-Host "  WASAPI COM 创建 IMMDeviceEnumerator 成功 (链路通畅)" -ForegroundColor Green
    } else {
        Write-Host "  WASAPI COM 失败 rc=$rc (设备指示灯麦克风 WASAPI 路径不可用)" -ForegroundColor Red
    }
    Write-Host ""
}

Write-Host "===== 总结 =====" -ForegroundColor Cyan
Write-Host "请把整段输出复制给开发者。重点关注:"
Write-Host "  - 摄像头每行错误码列: 三列全 0 = 空闲; 任意列 = 5/32/170 = 占用(取决于驱动返回)"
Write-Host "  - 如果你开了虚拟摄像头(OBS / ev录频 / ManyCam),但下面行全 0,就是漏检"
Write-Host "  - 麦克风 waveIn 行: 0 = 空闲, 10 / 33 = 占用, 其它 = 驱动不支持独占(需要 WASAPI)"
Write-Host "  - WASAPI 行: 0 = 链路通, 非 0 = 该路径不可用,需要 fall back 到 waveIn"
