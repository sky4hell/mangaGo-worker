@echo off
chcp 65001 >nul
title mangaGo Worker 安装

echo ==========================================
echo   mangaGo Worker 安装
echo ==========================================
echo.

REM 检查 Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/2] 安装依赖...
pip install -r requirements.txt -q

echo [2/2] 启动 Worker...
echo.
echo 安装完成！现在启动 Worker...
echo.
start "" python worker.py
