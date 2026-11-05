@echo off
setlocal
cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo Run this file as administrator: right click - Run as administrator.
    pause
    exit /b 1
)

set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
    echo pythonw.exe not found. Install Python from python.org and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

set "PY="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"

echo [1/4] Generating config.json and reading connection info...
if defined PY (
    "%PY%" "%~dp0server.py" --init
)

echo.
echo [2/4] Firewall rules for port 8765 (TCP + UDP) - local network and Tailscale only
netsh advfirewall firewall delete rule name="PC Control" >nul 2>&1
netsh advfirewall firewall delete rule name="PC Control UDP" >nul 2>&1
netsh advfirewall firewall add rule name="PC Control" dir=in action=allow protocol=TCP localport=8765 remoteip=LocalSubnet,100.64.0.0/10 >nul
netsh advfirewall firewall add rule name="PC Control UDP" dir=in action=allow protocol=UDP localport=8765 remoteip=LocalSubnet,100.64.0.0/10 >nul
if errorlevel 1 (
    echo Could not add the firewall rule.
    pause
    exit /b 1
)

echo [3/4] Hidden autostart at every Windows login
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\PC Control Server.lnk'); $s.TargetPath='%PYW%'; $s.Arguments=[char]34+'%~dp0server.py'+[char]34; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Save()"
if errorlevel 1 (
    echo Could not create the autostart shortcut.
    pause
    exit /b 1
)

echo [4/4] Restarting background server...
taskkill /F /IM pythonw.exe >nul 2>&1
start "" "%PYW%" "%~dp0server.py"
timeout /t 2 >nul

echo.
echo Done! The server is running in the background and will start automatically at login.
echo Copy the IP and Token printed above (or from config.json) into the Android app.
echo Log file: server.log.
echo.
pause
