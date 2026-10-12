@echo off
setlocal
set "APP=%~dp0scryfall_cards_gui.pyw"
if not exist "%APP%" (
    echo GUI program was not found. Keep this launcher beside scryfall_cards_gui.pyw.
    pause
    exit /b 1
)
set "LAUNCH_CODE=import sys,tkinter,subprocess; from pathlib import Path; p=Path(sys.executable); w=p.with_name('pythonw.exe'); subprocess.Popen([str(w if w.exists() else p),sys.argv[1]])"
py -3 -c "%LAUNCH_CODE%" "%APP%" >nul 2>&1
if not errorlevel 1 exit /b 0
python -c "%LAUNCH_CODE%" "%APP%" >nul 2>&1
if not errorlevel 1 exit /b 0
set "DSH_PYTHON=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if exist "%DSH_PYTHON%" (
    "%DSH_PYTHON%" -c "%LAUNCH_CODE%" "%APP%" >nul 2>&1
    if not errorlevel 1 exit /b 0
)
echo Python 3 with Tkinter was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Include Tcl/Tk and add Python to PATH during installation.
pause
exit /b 1
