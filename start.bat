@echo off
cd /d "%~dp0"

:: Prefer 'py' launcher (installed with Python for Windows)
where py >nul 2>&1
if not errorlevel 1 (
    set PYTHON=py
    goto :check_paramiko
)

where python >nul 2>&1
if not errorlevel 1 (
    set PYTHON=python
    goto :check_paramiko
)

echo [ERROR] Python is not installed or not in PATH.
echo         Download from https://www.python.org/
pause
exit /b 1

:check_paramiko
%PYTHON% -c "import paramiko" >nul 2>&1
if errorlevel 1 (
    echo Installing paramiko...
    %PYTHON% -m pip install paramiko
)

:: pyw/pythonw has no console, so no empty cmd window stays open
where pyw >nul 2>&1
if not errorlevel 1 (
    start "" pyw ssh_manager.py
) else (
    start "" pythonw ssh_manager.py
)
