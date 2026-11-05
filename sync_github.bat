@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo   Sync server to GitHub
echo ========================================================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo [!] Git is not installed. Download from: https://git-scm.com/
    pause
    exit /b 1
)

set "MSG="
set /p MSG=Commit message (Enter = Update): 
if "%MSG%"=="" set "MSG=Update"

echo [1/3] Committing...
git add -A
git commit -m "%MSG%"

echo [2/3] Getting changes from GitHub...
git pull --rebase origin main
if errorlevel 1 (
    echo.
    echo [!] Rebase conflict. Nothing was pushed.
    echo     Fix the files, then run: git rebase --continue
    echo     Or cancel with: git rebase --abort
    pause
    exit /b 1
)

echo [3/3] Pushing...
git push origin main
if errorlevel 1 (
    echo.
    echo [!] Push failed. Check internet or access to the repository.
    echo     Force push is NOT used on purpose, so nothing gets overwritten.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   [OK] Done. Server code pushed.
echo ========================================================
pause
