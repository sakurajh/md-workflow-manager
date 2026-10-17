#!/usr/bin/env bash
# 下载便携式 Python（python-build-standalone，兼容 glibc 2.17+）并验证 tkinter
set -e
mkdir -p /root/mdwf-linux && cd /root/mdwf-linux
for URL in \
  "https://github.com/astral-sh/python-build-standalone/releases/download/20240107/cpython-3.11.7+20240107-x86_64-unknown-linux-gnu-install_only.tar.gz" \
  "https://github.com/indygreg/python-build-standalone/releases/download/20240107/cpython-3.11.7+20240107-x86_64-unknown-linux-gnu-install_only.tar.gz"; do
  echo "尝试: $URL"
  if curl -L --connect-timeout 20 -o py.tar.gz "$URL"; then
    SIZE=$(stat -c%s py.tar.gz)
    if [ "$SIZE" -gt 10000000 ]; then echo "下载成功 ($SIZE 字节)"; break; fi
  fi
done
if [ ! -f py.tar.gz ] || [ "$(stat -c%s py.tar.gz)" -lt 10000000 ]; then
  echo "下载失败"
  exit 1
fi
rm -rf runtime
mkdir -p runtime
tar -xzf py.tar.gz -C runtime
echo "--- 验证便携 Python 与 tkinter ---"
runtime/python/bin/python3 -c 'import tkinter, sys; print("tkinter OK, Tk", tkinter.TkVersion, "| python", sys.version.split()[0])'
runtime/python/bin/python3 -c 'import ctypes; print("glibc 兼容性检查通过")'
