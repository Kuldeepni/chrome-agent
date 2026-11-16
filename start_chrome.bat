@echo off
REM ---------------------------------------------------------------------------
REM Start Google Chrome with remote debugging enabled for chrome-agent.
REM Close all running Chrome windows first, then run this script.
REM ---------------------------------------------------------------------------
setlocal

set "CHROME_PORT=9222"
set "PROFILE_DIR=%~dp0chrome-profile"

set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_EXE%" set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_EXE%" set "CHROME_EXE=%LocalAppData%\Google\Chrome\Application\chrome.exe"

if not exist "%CHROME_EXE%" (
    echo Could not find Chrome. Edit this script to set CHROME_EXE manually.
    exit /b 1
)

if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"

echo Starting Chrome with remote debugging on port %CHROME_PORT%
echo Profile: %PROFILE_DIR%
start "" "%CHROME_EXE%" --remote-debugging-port=%CHROME_PORT% --user-data-dir="%PROFILE_DIR%"

endlocal
