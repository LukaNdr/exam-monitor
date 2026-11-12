@echo off
chcp 65001 >nul
cd /d "%~dp0"
python exam_monitor.py %*
pause
