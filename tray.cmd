@echo off
setlocal
set "PYW=pythonw"
where pythonw >nul 2>nul || set "PYW=python"
start "" %PYW% "%~dp0home\tray.py"