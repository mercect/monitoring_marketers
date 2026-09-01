@echo off
REM Double-click this file to open the PI (Principal Investigators) dashboard.
REM It runs on port 8502, so it can be open at the same time as the
REM call-tracking monitor (START_DASHBOARD.bat, port 8501).
cd /d "%~dp0"
set "PYEXE=C:\Users\merce\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo Starting the PI dashboard... your browser will open at http://localhost:8502
echo Keep this window open while you use it. Close it (or press Ctrl+C) to stop.
"%PYEXE%" -m streamlit run pi_app.py --server.port 8502
pause
