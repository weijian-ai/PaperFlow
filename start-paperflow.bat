@echo off
chcp 65001 >nul 2>&1
setlocal EnableExtensions EnableDelayedExpansion
title PaperFlow Launcher

rem ============================================================
rem  PaperFlow one-click launcher
rem  Steps: locate project -> find uv -> sync deps -> start daemon
rem  Then show a menu: TUI / chat / one-shot task / status / stop
rem ============================================================

rem Let Python print UTF-8 so Chinese text renders correctly
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

rem ---- Project dir: prefer the folder containing this script ----
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

if not exist "%ROOT%\pyproject.toml" (
    if exist "D:\Project\soft\PaperFlow\pyproject.toml" set "ROOT=D:\Project\soft\PaperFlow"
)

if not exist "%ROOT%\pyproject.toml" (
    echo.
    echo [ERROR] PaperFlow project not found ^(pyproject.toml missing^).
    echo         Put this .bat in the PaperFlow project root and run again.
    echo         Detected dir: %ROOT%
    echo.
    pause
    exit /b 1
)

cd /d "%ROOT%"

rem ---- Locate uv ----
set "UV="
where uv >nul 2>&1 && set "UV=uv"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%USERPROFILE%\.cargo\bin\uv.exe" set "UV=%USERPROFILE%\.cargo\bin\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" set "UV=%LOCALAPPDATA%\Programs\uv\uv.exe"

if not defined UV (
    echo.
    echo [ERROR] uv not found; cannot prepare the Python environment.
    echo.
    echo   Install uv first ^(either^):
    echo     1^) PowerShell:  powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo     2^) Download from https://docs.astral.sh/uv/
    echo.
    echo   Then reopen this window and run the script again.
    echo.
    pause
    exit /b 1
)

echo ============================================================
echo   PaperFlow Launcher
echo ============================================================
echo   Project : %ROOT%
echo   uv      : %UV%
echo.

rem ---- First run: sync dependencies ----
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [1/3] First run: installing dependencies ^(needs network, 1-3 min^)...
    "%UV%" sync
    if errorlevel 1 (
        echo.
        echo [ERROR] Dependency install failed. Check your network and retry.
        pause
        exit /b 1
    )
    echo       Dependencies installed.
) else (
    echo [1/3] Dependencies ready.
)

rem ---- Check .env ----
if not exist "%ROOT%\.env" (
    echo.
    echo [WARN] .env not found; creating it from .env.example.
    if exist "%ROOT%\.env.example" copy /y "%ROOT%\.env.example" "%ROOT%\.env" >nul
    echo        Edit .env and fill in PAPERFLOW_LLM_API_KEY before running.
    echo.
    pause
)

echo [2/3] Checking LLM configuration...
"%UV%" run python -c "from paperflow.core.config import get_config as g; c=g(); print('       model   :', c.llm.default_model); print('       endpoint:', c.llm.provider, c.llm.base_url); print('       api key :', ('configured ' + c.llm.api_key[:10] + '...') if c.llm.api_key else 'MISSING!')" 2>nul
if errorlevel 1 echo       [WARN] Failed to read config; please check .env syntax.

rem ---- Start daemon ----
echo [3/3] Starting flow-core daemon...
rem Only "pong" proves the daemon is reachable; "flow core status" prints the word
rem "running" even when it says "not running", so never grep that.
"%UV%" run flow ping 2>nul | findstr /i "pong" >nul
if not errorlevel 1 (
    echo       Daemon already running.
) else (
    "%UV%" run flow core start
    rem Wait for the port; first import can be slow, up to ~40s.
    rem Use ping -n instead of timeout: timeout fails when stdin is redirected.
    set "READY="
    for /l %%i in (1,1,20) do (
        if not defined READY (
            ping -n 3 127.0.0.1 >nul 2>&1
            "%UV%" run flow ping 2>nul | findstr /i "pong" >nul
            if not errorlevel 1 set "READY=1"
        )
    )
    if defined READY (
        echo       Daemon ready.
    ) else (
        echo       [WARN] Daemon not ready yet; use menu [4] to check later.
    )
)

"%UV%" run flow ping 2>nul | findstr /i "pong" >nul
if errorlevel 1 (
    echo.
    echo [WARN] Cannot ping the daemon; TUI/chat may fail to connect.
    echo        Log: %USERPROFILE%\.paperflow\logs\core.log
    echo.
)

rem Optional direct commands, so the launcher can also be scripted:
rem   start-paperflow.bat tui | chat | run "<goal>" | status | stop
if /i "%~1"=="tui" goto tui
if /i "%~1"=="chat" goto chat
if /i "%~1"=="run" goto do_run
if /i "%~1"=="status" goto status
if /i "%~1"=="stop" goto stop

:menu
echo.
echo ============================================================
echo   Choose an action
echo ============================================================
echo    [1]  TUI          Recommended; full UI (tools/events/approval)
echo    [2]  Chat         Type y/a/n/d to approve tool permissions
echo    [3]  One-shot     Run a single goal, then exit
echo    [4]  Status       Check whether the daemon is running
echo    [5]  Stop         Shut down the background daemon
echo    [6]  Edit .env    Open the config in Notepad
echo    [7]  Logs         Open the log folder
echo    [0]  Exit
echo ============================================================
set "CHOICE="
set /p "CHOICE=Enter a number: "
rem set /p leaves CHOICE empty at EOF (e.g. piped input); stop instead of spinning
if not defined CHOICE goto end

if "%CHOICE%"=="1" goto tui
if "%CHOICE%"=="2" goto chat
if "%CHOICE%"=="3" goto do_run
if "%CHOICE%"=="4" goto status
if "%CHOICE%"=="5" goto stop
if "%CHOICE%"=="6" goto edit
if "%CHOICE%"=="7" goto logs
if "%CHOICE%"=="0" goto end
echo Invalid choice, try again.
goto menu

:tui
echo.
echo Launching TUI... ^(Ctrl+C or quit key returns to this menu^)
"%UV%" run flow-tui
echo.
echo TUI exited.
goto menu

:chat
echo.
echo Chat mode. Type a message and press Enter.
echo   Tool approval: y=allow once  a=always allow  n=deny  d=always deny
echo   Type exit or press Ctrl+C to leave.
echo.
"%UV%" run flow chat
echo.
echo Chat ended.
goto menu

:do_run
echo.
rem Allow a goal passed on the command line: start-paperflow.bat run "your goal"
set "GOAL=%~2"
if defined GOAL goto run_go
set "GOAL="
set /p "GOAL=Describe the task ^(Enter to cancel^): "
if not defined GOAL goto menu

:run_go
echo.
echo Running task, please wait...
echo.
"%UV%" run flow run --goal "%GOAL%"
echo.
echo Task finished.
if defined NO_PAUSE goto menu
pause
goto menu

:status
echo.
"%UV%" run flow core status
echo.
echo Recent event files:
dir /b /o-d "%USERPROFILE%\.paperflow\sessions\*\runs\*\events.jsonl" 2>nul | findstr /n "." | findstr /b "1:" 
echo.
if defined NO_PAUSE goto menu
pause
goto menu

:stop
echo.
"%UV%" run flow core stop
echo.
if defined NO_PAUSE goto menu
pause
goto menu

:edit
if exist "%ROOT%\.env" (
    notepad "%ROOT%\.env"
) else (
    echo .env not found.
    if not defined NO_PAUSE pause
)
goto menu

:logs
if exist "%USERPROFILE%\.paperflow\logs" (
    start "" explorer "%USERPROFILE%\.paperflow\logs"
) else (
    echo Log folder does not exist yet: %USERPROFILE%\.paperflow\logs
    if not defined NO_PAUSE pause
)
goto menu

:end
echo.
echo Note: the daemon is still running in the background.
echo To stop it, run this script again and choose [5].
echo Bye.
echo.
if not defined NO_PAUSE ping -n 4 127.0.0.1 >nul 2>&1
endlocal
exit /b 0
