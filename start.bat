@echo off
chcp 65001 >nul
title mangaGo Worker
set WORKER_API=https://zalomanga.com/api
set VENV_PYTHONW=%~dp0..\manga-image-translator\venv\Scripts\pythonw.exe
set TRANSLATOR_DIR=%~dp0..\manga-image-translator

REM 1. Translator service (GPU, port 8001)
start "Manga-Translator" /D "%TRANSLATOR_DIR%" "%VENV_PYTHONW%" server\main.py --port 8001 --use-gpu --models-ttl=3600

REM 2. Worker (GUI + polling)
start "" "%VENV_PYTHONW%" "%~dp0worker.py"

REM 3. Image service (port 7003)
start "" "%VENV_PYTHONW%" "%~dp0local_image_server.py"
