@echo off
chcp 65001 >nul
title mangaGo Worker
start "" "%~dp0..\manga-image-translator\venv\Scripts\python.exe" "%~dp0worker.py"
