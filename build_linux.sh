#!/usr/bin/env bash
# ============================================================================
# MD Workflow Manager —— Linux 构建脚本（适配 Rocky Linux 9.x / 9.2）
#
# 用法（在 Rocky 9.2 上执行）:
#   chmod +x build_linux.sh
#   ./build_linux.sh
#
# 脚本会:
#   1. 检查/安装依赖（python3、python3-pip、python3-tkinter）
#   2. 安装 PyInstaller 并渲染图标
#   3. 打包出单文件可执行程序 dist/MDWorkflowManager
#
# 重要: 请务必在目标系统（Rocky 9.2）上构建。
# glibc 更老的系统上编译的二进制可在 9.2 运行；
# 在更新的系统（如 Ubuntu 24.04+）上编译的无法在 9.2 运行。
# 也可用仓库内的 Dockerfile.rocky92 做可复现构建。
# ============================================================================
set -e
cd "$(dirname "$0")"

echo "== [1/3] 检查依赖 =="
MISSING=()
command -v python3 >/dev/null 2>&1 || MISSING+=("python3")
if command -v python3 >/dev/null 2>&1; then
    python3 -c "import tkinter" 2>/dev/null || MISSING+=("python3-tkinter")
fi
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "缺少: ${MISSING[*]}"
    if command -v dnf >/dev/null 2>&1; then
        echo "尝试用 dnf 安装（需要 sudo/root 权限）..."
        dnf install -y "${MISSING[@]}" || sudo dnf install -y "${MISSING[@]}"
    elif command -v apt-get >/dev/null 2>&1; then
        apt-get update && apt-get install -y python3-tk || sudo apt-get install -y python3-tk
    else
        echo "请手动安装以上依赖后重试。"
        exit 1
    fi
fi

echo "== [2/3] 安装 PyInstaller / CustomTkinter 并渲染图标 =="
python3 -m pip install --quiet pyinstaller \
    || python3 -m pip install --quiet --user pyinstaller
python3 -m pip install --quiet 'customtkinter==6.0.0' \
    || python3 -m pip install --quiet --user 'customtkinter==6.0.0'
python3 make_icon.py

echo "== [3/3] 打包 =="
python3 -m PyInstaller --noconfirm --clean --onefile \
    --add-data "md_icon_64.ppm:." \
    --collect-all customtkinter \
    --name MDWorkflowManager main.py

echo ""
echo "== 自检 =="
./dist/MDWorkflowManager --selftest || true

echo ""
echo "构建完成: dist/MDWorkflowManager"
echo "运行方式: ./dist/MDWorkflowManager   （GUI 需要图形桌面环境）"
echo "提示: Linux 版模拟在本机运行，缺 GROMACS 时软件内可自动安装。"
