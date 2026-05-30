@echo off
echo ===========================
echo  Jarvis Setup
echo ===========================
echo.
echo [1/2] Installing Python dependencies...
cd /d "%~dp0backend"
pip install -r requirements.txt
cd /d "%~dp0"
echo.
echo [2/2] Checking Codex CLI...
where codex.cmd >nul 2>&1
if %errorlevel% neq 0 (
    echo Codex CLI tidak ditemukan. Installing...
    npm install -g @openai/codex
) else (
    echo Codex CLI sudah terinstall.
)
echo.
echo ===========================
echo  Setup selesai!
echo ===========================
echo.
echo Langkah selanjutnya:
echo 1. Edit backend\.env jika perlu ubah CODEX_DEFAULT_PROJECT
echo 2. Jalankan start.bat
echo 3. Buka browser HP: http://[IP_LAPTOP]:8000
echo    Cari IP laptop dengan: ipconfig
echo.
pause
