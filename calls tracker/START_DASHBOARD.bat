@echo off
REM Double-click this file to open the dashboard in your browser.
cd /d "%~dp0"
set "PYEXE=C:\Users\merce\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo Starting the dashboard... your browser will open at http://localhost:8501
echo Keep this window open while you use it. Close it (or press Ctrl+C) to stop.
"%PYEXE%" -m streamlit run app.py
pause
