#!/usr/bin/env bash
# build_macos.sh — 在 macOS 上把 ucopy_server 打包成 arm64 单文件可执行
#
# 产物: dist/ucopy-server (单文件，可直接 ./ucopy-server 跑)
# 平台: macOS arm64 (Apple Silicon)。不再支持 Intel x86_64。
# Python: conda base (Python 3.13 + pyinstaller 6.x) 或 PATH 上的 python3
#
# 用法:
#   bash build_macos.sh    # 一键构建 arm64
#
# 注意:
#   - PyInstaller 会调用 openssl（cert.py 用）→ 必须 macOS 自带 openssl
#   - 自签证书生成走系统 openssl → 服务器首次启动需要 openssl 在 PATH
#   - Intel Mac 不再支持（PyInstaller 不支持 macOS Rosetta 翻译构建 x86_64）

set -e

cd "$(dirname "$0")"

# Apple Silicon only — M 系列芯片
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: build_macos.sh 必须在 macOS 上跑" >&2
    exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: 本项目不再支持 Intel Mac。请在搭载 M 系列芯片的 Mac 上构建。" >&2
    echo "       (Python 3.13 + PyInstaller 6.x 不支持跨 arch Rosetta 构建)" >&2
    exit 1
fi

# Python 解析顺序：
#   1. $PYTHON 环境变量（CI 可设）
#   2. /opt/anaconda3/bin/python（本地 mac dev 机器常用位置）
#   3. python3（PATH 上的任意 python；CI 必走这条）
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]] && [[ -x /opt/anaconda3/bin/python ]]; then
    PYTHON=/opt/anaconda3/bin/python
fi
if [[ -z "$PYTHON" ]]; then
    PYTHON="$(command -v python3)"
fi
if [[ -z "$PYTHON" ]] || [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: 找不到 python3" >&2
    echo "       设 PYTHON=/path/to/python3 或装 pyinstaller" >&2
    exit 1
fi

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
    echo "ERROR: $PYTHON 没装 pyinstaller。先跑:" >&2
    echo "       $PYTHON -m pip install pyinstaller" >&2
    exit 1
fi

# 清理旧产物
rm -rf build dist
echo "[clean] build/ dist/ 已清理"

echo
echo "============================================="
echo "[build] target arch: arm64 (Apple Silicon only)"
echo "============================================="

"$PYTHON" -m PyInstaller \
    --onefile \
    --name ucopy-server \
    --target-arch arm64 \
    --console \
    --add-data "ucopy_server/static:ucopy_server/static" \
    --collect-submodules ucopy_server \
    --noconfirm \
    ucopy_server/server.py

bin="dist/ucopy-server"
if [[ ! -x "$bin" ]]; then
    echo "ERROR: 构建失败：$bin 不存在" >&2
    exit 1
fi
file "$bin"

# 重命名为发布产物约定
mv "$bin" dist/UCopy-Server-macos-arm64
chmod +x dist/UCopy-Server-macos-arm64

echo
echo "============================================="
echo "[done] 单文件已生成: dist/UCopy-Server-macos-arm64"
echo "[usage] ./dist/UCopy-Server-macos-arm64 --help"
echo "[usage] ./dist/UCopy-Server-macos-arm64 --port 8443 --data-dir ~/UCopyServer"
echo "============================================="

ls -la dist/UCopy-Server-macos-arm64
du -sh dist/UCopy-Server-macos-arm64