@echo off
REM AI Job Hunter - Persistent Worker Daemon Launcher
echo Starting AI Job Hunter Continuous Worker Daemon...
cd /d "%~dp0\.."
python agent\main.py --worker
pause
