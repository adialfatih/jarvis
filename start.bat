@echo off
echo ===========================
echo  Starting Jarvis...
echo ===========================
cd /d "%~dp0backend"
if "%JARVIS_PORT%"=="" set JARVIS_PORT=8300
uvicorn main:app --host 0.0.0.0 --port %JARVIS_PORT%
