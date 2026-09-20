@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

set "PY="
if exist "%USERPROFILE%\anaconda3\envs\travel\python.exe" set "PY=%USERPROFILE%\anaconda3\envs\travel\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\anaconda3\envs\travel\python.exe" set "PY=%LOCALAPPDATA%\anaconda3\envs\travel\python.exe"
if not defined PY if exist "D:\Anaconda\envs\travel\python.exe" set "PY=D:\Anaconda\envs\travel\python.exe"
if not defined PY if exist "D:\Anaconda3\envs\travel\python.exe" set "PY=D:\Anaconda3\envs\travel\python.exe"
if not defined PY if exist "C:\ProgramData\Anaconda3\envs\travel\python.exe" set "PY=C:\ProgramData\Anaconda3\envs\travel\python.exe"
if not defined PY if exist "C:\anaconda3\envs\travel\python.exe" set "PY=C:\anaconda3\envs\travel\python.exe"
if not defined PY if exist "%USERPROFILE%\miniconda3\envs\travel\python.exe" set "PY=%USERPROFILE%\miniconda3\envs\travel\python.exe"

if not defined PY (
  echo [ERROR] Travel env python not found.
  echo Please run in Anaconda Prompt: conda activate travel, then python api_server.py
  pause
  exit /b 1
)

echo [OK] Using python: %PY%
echo [INFO] Starting AI Travel Agent at http://localhost:8000 ...
start "AI Travel Agent" /min "%PY%" api_server.py
timeout /t 3 >nul
echo [OK] Started. Open http://localhost:8000 in browser.
echo [INFO] You can close this window; service runs in background.
pause
