@echo off
setlocal
cd /d "%~dp0"
title 黑板小喇叭

if exist "黑板小喇叭.exe" (
    start "" "黑板小喇叭.exe"
    exit /b
)

if exist "dist_release\黑板小喇叭\黑板小喇叭.exe" (
    start "" "dist_release\黑板小喇叭\黑板小喇叭.exe"
    exit /b
)

if exist "dist\黑板小喇叭\黑板小喇叭.exe" (
    start "" "dist\黑板小喇叭\黑板小喇叭.exe"
    exit /b
)

python receiver.py
if errorlevel 1 (
    echo 程序已退出。
    pause
)
