@echo off
rem Jarvis launcher (Phase 7A). Started minimized at sign-in by the Startup-folder entry
rem that `jarvis --autostart on` creates. Runs voice mode; if Jarvis crashes, restarts it
rem (at most 4 restarts, 10 seconds apart). A normal exit (Ctrl+C or /exit) is not restarted.
setlocal
cd /d "%~dp0.."
set RESTARTS=0
:run
".venv\Scripts\jarvis.exe" --voice
set CODE=%ERRORLEVEL%
if "%CODE%"=="0" goto end
set /a RESTARTS+=1
if %RESTARTS% GEQ 5 goto giveup
echo Jarvis stopped unexpectedly (exit code %CODE%). Restart %RESTARTS% of 4 in 10 seconds...
"%SystemRoot%\System32\timeout.exe" /t 10 /nobreak >nul
goto run
:giveup
echo Jarvis stopped unexpectedly 5 times; not restarting. Details: logs\jarvis.log
pause
:end
endlocal
