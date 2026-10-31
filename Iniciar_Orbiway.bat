@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw.exe "%~dp0Iniciar_Orbiway.py"
) else (
    start "Orbiway" python.exe "%~dp0Iniciar_Orbiway.py"
)
endlocal
