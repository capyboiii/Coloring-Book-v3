@echo off
echo ============================================================
echo   Coloring Book Generator - Lulu Dashboard Server
echo ============================================================
echo.
echo Closing any previous server running on port 8000...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do taskkill /F /PID %%a 2>nul

echo Starting FastAPI server at http://127.0.0.1:8000 ...
start "" http://127.0.0.1:8000
python server.py
pause
