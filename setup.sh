#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/backend"

echo "==========================="
echo " Jarvis Setup (Linux)"
echo "==========================="

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo ">> backend/.env dibuat dari contoh — WAJIB diisi (token, Telegram, PROJECT_ROOTS)."
fi

echo
echo "Setup selesai. Jalankan: ./start.sh"
echo "Akses dari HP: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000 (atau IP Tailscale)"
