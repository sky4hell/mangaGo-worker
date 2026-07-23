@echo off
chcp 65001 >nul
title mangaGo Worker

REM 确保翻译服务已启动
echo 检查翻译服务 (localhost:8001)...
curl -s http://localhost:8001/docs >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 翻译服务未启动，请先运行翻译服务的 start.bat
    choice /c yn /m "是否仍然启动 Worker？(Y/N)"
    if errorlevel 2 exit /b
)

start "" python worker.py
