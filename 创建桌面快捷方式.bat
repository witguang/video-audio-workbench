@echo off
rem Removed chcp 65001 / PYTHONUTF8=1: they conflict with the native
rem GBK console codepage on Chinese Windows and garble Chinese output.
rem This bat is pure ASCII; all Chinese comes from create_shortcut.py's
rem print() calls, which Python renders correctly by default.
cd /d "%~dp0"
python create_shortcut.py
echo.
pause
