@echo off
setlocal

REM --- ADJUSTED PATHS (keep the quotes) ---
set "PYTHON_EXE=D:\04_Met_Ocean\02_Python\10_BOUY_GPS_Tracker\.venv\Scripts\pythonw.exe"
set "APP_DIR=D:\04_Met_Ocean\02_Python\10_BOUY_GPS_Tracker"

REM --- go to the app folder so relative paths work ---
cd /d "%APP_DIR%"

:loop
"%PYTHON_EXE%" gui.py
REM If it crashes/exits, wait 2s and relaunch
timeout /t 2 /nobreak >nul
goto loop
