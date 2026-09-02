@echo off
REM Double-click to open the PI dashboard (Recruitment + Attrition).
REM
REM Runs from THIS folder, which is the GitHub clone and the deploy source.
REM Do NOT use the copy in  Desktop\attrition system\dashboard\  -- that folder
REM is an older fork; edits there never reach the deployed app.
REM
REM Port 8502, so it can run alongside the call-tracking monitor on 8501.
cd /d "%~dp0"
set "PYEXE=C:\Users\merce\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
if not exist "%~dp0.streamlit\secrets.toml" (
  echo.
  echo   No .streamlit\secrets.toml in this folder.
  echo   It is gitignored, so a fresh clone does not have it. Copy it from
  echo   Desktop\attrition system\dashboard\.streamlit\secrets.toml
  echo.
  pause
  exit /b 1
)
echo Starting the PI dashboard at http://localhost:8502
echo Keep this window open while you use it. Close it (or press Ctrl+C) to stop.
"%PYEXE%" -m streamlit run pi_app.py --server.port 8502
pause
