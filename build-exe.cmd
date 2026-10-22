@echo off
REM Build devtask as a folder (onedir). Requires: pip install pyinstaller
REM onedir starts in under a second; onefile unpacks 26 MB on every launch (~30 s).
setlocal
cd /d "%~dp0"

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --windowed ^
  --name devtask ^
  --hidden-import devtask ^
  --hidden-import devtask_gui ^
  devtask.py

if errorlevel 1 exit /b 1
copy /y devtask.config.json dist\devtask\devtask.config.json >nul 2>&1
echo.
echo Built: %~dp0dist\devtask\devtask.exe
echo Distribute the whole dist\devtask folder (zip it): devtask.exe + _internal\
