@echo off
chcp 65001 >nul
title mangaGo Worker
echo 启动 mangaGo Worker...
start "" "E:\mangoGo-comic\manga-image-translator\venv\Scripts\python.exe" "E:\mangoGo-comic\mangaGo-worker\worker.py"
