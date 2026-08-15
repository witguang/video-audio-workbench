@echo off
rem 创建桌面快捷方式（无命令行窗口：pythonw 无控制台，窗口一闪即关）
cd /d "%~dp0"
start "" /b pythonw.exe create_shortcut.py
exit
