@echo off
chcp 65001 >nul
title mangaGo Worker
set WORKER_API=https://zalomanga.com/api
start "" "%~dp0..\manga-image-translator\venv\Scripts\python.exe" "%~dp0worker.py"
start "" "%~dp0..\manga-image-translator\venv\Scripts\python.exe" "%~dp0local_image_server.py"
