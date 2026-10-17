#!/usr/bin/env bash
# 组装 Linux 便携版软件包并自检
set -e
SRC="/mnt/c/Users/赵建华/AppData/Roaming/CherryStudio/Data/Agents/system/2026-09-23/5f728f62-0587-46f6-b525-d0392d3efdfa/md-workflow-manager"
BASE=/root/mdwf-linux
PKG=$BASE/MDWorkflowManager-linux64

rm -rf "$PKG"
mkdir -p "$PKG/app"

# 1) 应用源码
cp -r "$SRC/mdflow" "$PKG/app/mdflow"
cp "$SRC/main.py" "$SRC/md_icon_64.ppm" "$SRC/README.md" "$PKG/app/"
find "$PKG/app" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 2) 便携运行时
cp -r "$BASE/runtime" "$PKG/runtime"

# 2.5) 界面依赖 CustomTkinter（GUI 必需；含 pillow/darkdetect/packaging）
runtime_deps() { "$PKG/runtime/python/bin/python3" -m pip "$@"; }
runtime_deps install --quiet 'customtkinter==6.0.0' \
    || { echo "pip 源失败，尝试镜像"; PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple runtime_deps install --quiet 'customtkinter==6.0.0'; }
"$PKG/runtime/python/bin/python3" -c "import customtkinter; print('customtkinter', customtkinter.__version__, 'OK')"

# 3) 启动器（GUI + CLI）
cat > "$PKG/MDWorkflowManager" <<'EOF'
#!/bin/bash
# MD Workflow Manager 图形界面（需要图形桌面）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/runtime/python/bin/python3" "$DIR/app/main.py" "$@"
EOF
cat > "$PKG/MDWorkflowManager-cli" <<'EOF'
#!/bin/bash
# MD Workflow Manager 控制台版（SSH / nohup / tmux，无桌面可用）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/runtime/python/bin/python3" "$DIR/app/main.py" --cli "$@"
EOF
chmod +x "$PKG/MDWorkflowManager" "$PKG/MDWorkflowManager-cli"

# 4) 使用说明
cat > "$PKG/使用说明.txt" <<'EOF'
MD Workflow Manager（Linux 便携版 v0.4.0）

【图形界面】（需要图形桌面，GNOME/X11 均可）
    ./MDWorkflowManager

【控制台模式】（无桌面也能跑，适合 SSH / nohup / tmux）
    ./MDWorkflowManager-cli <CHARMM-GUI目录或压缩包> -o 输出目录
    例: ./MDWorkflowManager-cli ~/charmm-gui.tgz -o ~/MD_sim
    默认完全按 CHARMM-GUI 原始 mdp 参数运行；
    可用 --em-ps/--equil-ps/--prod-ns 覆盖时长，--help 看全部选项。
    后台挂机:  nohup ./MDWorkflowManager-cli ... > run.log 2>&1 &
    取消: 运行中按 Ctrl+C（会终止当前 gmx 进程并保留已完成阶段）

环境: Rocky 9.2 / glibc 2.17+ 通用，无需安装 Python。
中文显示: 建议安装中文字体
    dnf install google-noto-sans-cjk-ttc-fonts
GROMACS: 缺 gmx 时（CLI 加 --auto-install-gmx，GUI 点"自动安装"）
    软件会自动下载 micromamba 并安装，无需管理员权限。

【超算用户】
    1. 先查模块:  module avail gromacs
    2. 生成作业脚本（自动加载模块 + 申请 GPU/核数）:
       ./MDWorkflowManager-cli ~/charmm-gui.tgz -o ~/MD_sim \
           --module-load "gromacs/2023.3 cuda/12.2" \
           --scheduler slurm --partition gpu --cpus 16 --ngpus 1 --submit
       （PBS 集群用 --scheduler pbs；不加 --submit 则只生成脚本手动提交）
    3. 登录节点快速验证可加 --scheduler none 直接短跑。

自检:  ./MDWorkflowManager --selftest
EOF

# 5) 自检（用打包内的 Python 跑完整自检，不需要 GUI）
cd "$PKG"
echo "=== 便携版自检 ==="
runtime/python/bin/python3 app/main.py --selftest && echo "SELFTEST PASS" || echo "SELFTEST FAIL"
echo "=== tkinter 可用性 ==="
runtime/python/bin/python3 -c "import tkinter; print('GUI 依赖 tkinter OK')"

# 6) 打包 tar.gz（保留执行权限）
cd "$BASE"
tar -czf MDWorkflowManager-linux64.tar.gz MDWorkflowManager-linux64
ls -la MDWorkflowManager-linux64.tar.gz
echo "=== 包内容概览 ==="
tar -tzf MDWorkflowManager-linux64.tar.gz | head -8
tar -tzf MDWorkflowManager-linux64.tar.gz | wc -l
