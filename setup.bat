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
echo 1. Copy backend\.env.example jadi backend\.env lalu isi:
echo    - JARVIS_AUTH_TOKEN (token rahasia)
echo    - TELEGRAM_BOT_TOKEN bot BARU khusus mesin ini (1 bot per mesin!)
echo    - PROJECT_ROOTS
echo 2. Jalankan start.bat
echo 3. Di app HP, tambah mesin: http://[IP_TAILSCALE_LAPTOP]:8300 + token
echo    Cek IP Tailscale dengan: tailscale ip -4
echo.
pause
