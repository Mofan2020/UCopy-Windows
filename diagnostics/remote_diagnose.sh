#!/usr/bin/env bash
# diagnostics/remote_diagnose.sh
# 远程 SSH 到 Windows(假设你的 Windows 装了 OpenSSH Server)后由我执行:
#   1. 检测 UCopy.exe 在不在,启动并看小圆点是否变色
#   2. 跑 probe.ps1 输出摄像头/麦克风枚举 + 错误码
#   3. dump UCopy 进程的 stderr(我们 UCopy 没日志文件,只能看运行时 stdout — 但运行时没控制台)
#   4. 看设备管理器里摄像头类别数量
#
# 用法: ssh user@host 'bash -s' < remote_diagnose.sh
set -e

echo "===== 0. 基础环境 =====" 
whoami
ver
powershell -NoProfile -Command '$PSVersionTable.PSVersion'

echo ""
echo "===== 1. UCopy.exe 状态 ====="
# 找 UCopy
UCOPY_PATH=""
for cand in "C:\ProgramData\chocolatey\bin\UCopy.exe" \
            "C:\Program Files\UCopy\UCopy.exe" \
            "C:\Program Files (x86)\UCopy\UCopy.exe" \
            "$(cygpath -u "$USERPROFILE")/Desktop/UCopy.exe" \
            "$(cygpath -u "$USERPROFILE")/Downloads/UCopy.exe"; do
    if [ -e "$cand" ]; then UCOPY_PATH="$cand"; break; fi
done
echo "UCopy 路径: ${UCOPY_PATH:-未找到}"
if [ -z "$UCOPY_PATH" ]; then
    echo "(如果路径在别处,请告诉我)"
    exit 0
fi

echo ""
echo "===== 2. 当前摄像头占用情况 (用我带的 probe.ps1) ====="
# 把 probe.ps1 传到机器并执行
PROBE_LOCAL="diagnostics/probe.ps1"
PROBE_REMOTE="/tmp/probe.ps1"
scp "$PROBE_LOCAL" "${REMOTE_USER:-${USER}}@${HOSTNAME:-localhost}:${PROBE_REMOTE}" 2>/dev/null \
    || powershell -NoProfile -ExecutionPolicy Bypass -File "$PROBE_LOCAL"

echo ""
echo "===== 3. 当前是否有进程在用摄像头 (简化:列系统进程名) ====="
tasklist /FO CSV 2>/dev/null | head -50

echo ""
echo "===== 4. 测试:手动启动 UCopy,开/关 OBS 风格的虚拟摄像头,看指示灯 ====="
echo "(这一步我远程没法开 OBS,你自己跑 — 但探针输出我能看)"
echo ""
echo "探针输出已采集。请把上面截图/文字直接发回来,我直接定位错码。"
