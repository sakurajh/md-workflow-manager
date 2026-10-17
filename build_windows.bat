@echo off
rem ============================================================
rem  MD Workflow Manager - Windows exe 打包脚本
rem  用法: 双击运行，或在命令行执行 build_windows.bat
rem  输出: dist\MDWorkflowManager.exe（单文件，无需安装 Python）
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] 安装依赖 PyInstaller + CustomTkinter ...
python -m pip install pyinstaller customtkinter==6.0.0 || goto :err

echo [2/4] 生成图标 ...
python make_icon.py || goto :err

echo [3/4] 打包 ...
python -m PyInstaller --noconfirm --clean --onefile --windowed --icon app.ico --add-data "md_icon_64.ppm;." --collect-all customtkinter --name MDWorkflowManager main.py || goto :err

echo [4/4] 自检 ...
dist\MDWorkflowManager.exe --selftest

echo.
echo 打包完成: dist\MDWorkflowManager.exe
pause
exit /b 0

:err
echo.
echo 打包失败，请检查上方错误信息。
pause
exit /b 1
