@echo off
REM Double-click to verify the pipeline: data loads + rollup correctness.
cd /d "%~dp0"
set "PYEXE=C:\Users\merce\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
echo ================ 1) SETUP CHECK (data loads) ================
"%PYEXE%" validate_setup.py
echo.
echo ================ 2) CORRECTNESS CHECK (rollup) ==============
"%PYEXE%" test_rollup.py
echo.
pause
