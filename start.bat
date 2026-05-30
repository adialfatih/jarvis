@echo off
echo ===========================
echo  Starting Jarvis...
echo ===========================
cd /d "%~dp0backend"
uvicorn main:app --host 0.0.0.0 --port 8000
