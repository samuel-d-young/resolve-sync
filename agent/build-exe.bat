@echo off
REM Build the standalone Windows app into dist\Resolve Sync\ and zip it (~20s).
REM
REM Notes:
REM  - Do NOT exclude sqlite3: detect.py reads Resolve's project databases with
REM    it to auto-suggest media roots.
REM  - resolve_sync\google_client.json is optional. If present it is baked into
REM    the build so everyone you share the app with can just click "Sign in with
REM    Google" instead of visiting Google Cloud Console.

cd /d "%~dp0"

set "GOOGLE_ARG="
if exist "resolve_sync\google_client.json" (
  set "GOOGLE_ARG=--add-data resolve_sync\google_client.json;resolve_sync"
  echo Bundling Google client credentials into this build.
) else (
  echo No resolve_sync\google_client.json - users will each need their own Google client.
)

.venv\Scripts\python.exe -m PyInstaller ^
  --noconfirm --clean --noconsole --onedir ^
  --name "Resolve Sync" ^
  --icon app.ico ^
  --add-data "static;static" ^
  %GOOGLE_ARG% ^
  --hidden-import keyring.backends.Windows ^
  --hidden-import win32ctypes.core ^
  --hidden-import win32ctypes.core.ctypes ^
  --copy-metadata keyring ^
  --exclude-module tkinter --exclude-module unittest --exclude-module pydoc ^
  --exclude-module lib2to3 --exclude-module test ^
  --exclude-module watchfiles --exclude-module websockets --exclude-module httptools ^
  run.py

if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)

powershell -NoProfile -Command "Compress-Archive -Path 'dist\Resolve Sync' -DestinationPath 'dist\ResolveSync-Windows.zip' -Force"
echo.
echo Built: dist\Resolve Sync\Resolve Sync.exe
echo Ship:  dist\ResolveSync-Windows.zip
