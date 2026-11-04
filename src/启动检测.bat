@echo off
cd /d "%~dp0"
title TVBox Address Finder

where py >nul 2>nul
if not errorlevel 1 goto runpy

where python >nul 2>nul
if not errorlevel 1 goto runpython

echo Python 3 not found. Install it from https://www.python.org/downloads/
pause
exit /b

:runpy
start "" py -3w "%~dp0tvbox_finder.py"
exit /b

:runpython
start "" pythonw "%~dp0tvbox_finder.py"
exit /b
