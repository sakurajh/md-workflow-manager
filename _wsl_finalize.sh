#!/usr/bin/env bash
# 清理包内自检残留 → 重新打包 → 复制到 Windows dist-linux/
set -e
BASE=/root/mdwf-linux
PKG=$BASE/MDWorkflowManager-linux64
DEST="/mnt/c/Users/赵建华/AppData/Roaming/CherryStudio/Data/Agents/system/2026-09-23/5f728f62-0587-46f6-b525-d0392d3efdfa/md-workflow-manager/dist-linux"

rm -f "$PKG/mdwf_selftest_report.txt"
cd "$BASE"
rm -f MDWorkflowManager-linux64.tar.gz
tar -czf MDWorkflowManager-linux64.tar.gz MDWorkflowManager-linux64
mkdir -p "$DEST"
cp MDWorkflowManager-linux64.tar.gz "$DEST/"
ls -la "$DEST/"
echo "=== 校验 tar 完整性 ==="
tar -tzf "$DEST/MDWorkflowManager-linux64.tar.gz" | grep -E "MDWorkflowManager$|main.py|使用说明" | head -5
echo "OK"
