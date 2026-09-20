@echo off
chcp 65001 >nul 2>&1
echo [INFO] Stopping service on port 8000 ...
for /f "tokens=5" %%i in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
  taskkill /f /pid %%i >nul 2>&1 && echo [OK] Stopped PID %%i
)
echo [OK] Done. Port 8000 is released.
pause
