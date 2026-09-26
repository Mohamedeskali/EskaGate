@echo off
chcp 65001 >nul
title API Test Console
cd /d "%~dp0"

python api_web_dashboard_v2.py --port 8000 --open
pause
