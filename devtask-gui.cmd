@echo off
rem Launch the devtask GUI without a console window.
rem Keep this file ASCII-only: cmd.exe parses it as GBK and non-ASCII bytes can break lines.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "devtask_gui.py" %*
) else (
    start "" python "devtask_gui.py" %*
)
