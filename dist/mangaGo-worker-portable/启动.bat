@echo off
chcp 65001 >nul
title mangaGo Worker
echo 启动 mangaGo Worker...
echo 请确保翻译服务 (localhost:8001) 已在运行
start "" "%~dp0python\python.exe" "%~dp0worker.py"
