#!/usr/bin/env bash
# build_macos.sh — 在 macOS 上把 ucopy_server 打包成单文件可执行
#
# 产物: dist/ucopy-server (单文件，可直接 ./ucopy-server 跑)
# 平台: macOS (arm64 / x86_64 都支持)
# Python: conda base (Python 3.13 + pyinstaller 6.x)
#
# 用法:
#   bash build_macos.sh                   # 默认：当前 arch
#   TARGET_ARCH=arm64 bash build_macos.sh # 显式 arm64 (Apple Silicon)
#   TARGET_ARCH=x86_64 bash build_macos.sh # 显式 x86_64 (Intel)
#   UNIVERSAL=1 bash build_macos.sh       # 通用二进制 (arm64 + x86_64 合一)
#
# 注意:
#   - PyInstaller 会调用 openssl（cert.py 用）→ 必须 macOS 自带 openssl
#   - 自签证书生成走系统 openssl → 服务器首次启动需要 openssl 在 PATH
#   - 通用二进制需要两个单 arch 都构建后用 lipo 合并，体积会变大

set -e

cd "$(dirname "$0")"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: build_macos.sh 必须在 macOS 上跑" >&2
    exit 1
fi

PYTHON="${PYTHON:-/opt/anaconda3/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: 找不到 python3 ($PYTHON)" >&2
    echo "       改成你自己的路径或在 conda base 里装 pyinstaller" >&2
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

TARGET_ARCH="${TARGET_ARCH:-$(uname -m)}"

build_one_arch() {
    local arch="$1"
    echo
    echo "============================================="
    echo "[build] target arch: $arch"
    echo "============================================="

    "$PYTHON" -m PyInstaller \
        --onefile \
        --name ucopy-server \
        --target-arch "$arch" \
        --console \
        --add-data "ucopy_server/static:ucopy_server/static" \
        --collect-submodules ucopy_server \
        --noconfirm \
        ucopy_server/server.py

    local bin="dist/ucopy-server"
    if [[ ! -x "$bin" ]]; then
        echo "ERROR: 构建失败：$bin 不存在" >&2
        exit 1
    fi
    file "$bin"
    echo "[ok] $bin"
}

if [[ "${UNIVERSAL:-0}" == "1" ]]; then
    # 通用二进制：两个 arch 都构建 + lipo 合并
    build_one_arch arm64
    mv dist/ucopy-server dist/ucopy-server.arm64
    build_one_arch x86_64
    mv dist/ucopy-server dist/ucopy-server.x86_64
    lipo -create \
        -arch arm64 dist/ucopy-server.arm64 \
        -arch x86_64 dist/ucopy-server.x86_64 \
        -output dist/ucopy-server
    rm dist/ucopy-server.arm64 dist/ucopy-server.x86_64
    echo
    echo "============================================="
    echo "[ok] 通用二进制已生成: dist/ucopy-server"
    echo "============================================="
    lipo -info dist/ucopy-server
else
    build_one_arch "$TARGET_ARCH"
    echo
    echo "============================================="
    echo "[done] 单文件已生成: dist/ucopy-server"
    echo "[usage] ./dist/ucopy-server --help"
    echo "[usage] ./dist/ucopy-server --port 8443 --data-dir ~/UCopyServer"
    echo "============================================="
fi

ls -la dist/ucopy-server
du -sh dist/ucopy-server