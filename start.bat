@echo off
chcp 65001 >nul
title mangaGo Worker
start "" cmd /c "cd /d %~dp0 && %~dp0..\manga-image-translator\venv\Scripts\pythonw.exe worker.py"
