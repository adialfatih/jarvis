# PRD — Jarvis: Remote AI Coding Control dari HP

**Versi:** 0.4
**Tanggal:** 16 Juli 2026
**Status:** Fase 0, 1, 2 SELESAI + sebagian besar Fase 3 (teruji end-to-end di
PC Ubuntu). Sisa: setup laptop Windows + uji coba nyata oleh user.

> **Catatan penting (ditemukan saat implementasi):** satu bot Telegram hanya
> boleh dipakai SATU agent, karena `getUpdates` long-polling konflik (409) jika
> dikonsumsi dua proses. Solusi: **satu bot per mesin** (buat bot kedua via
> @BotFather untuk laptop Windows; `TELEGRAM_CHAT_ID` tetap sama).
> Port default agent diganti **8300** karena 8000 umum dipakai app dev (di PC
> Ubuntu sudah terpakai PHP).

---

## 1. Visi Produk

Sebuah tools yang berjalan di HP (browser/PWA) untuk mengendalikan sesi AI coding
(Claude Code & Codex CLI) di beberapa mesin sekaligus — laptop Windows 10 dan PC
Ubuntu — dari mana saja (tidak hanya LAN rumah).

Alur ideal dari sisi pengguna:

1. Buka app di HP → pilih mesin (Laptop Windows / PC Ubuntu).
2. Pilih project di mesin tersebut (atau buat folder baru).
3. Start sesi Claude Code atau Codex di project itu.
4. Kirim prompt (ketik atau suara/Whisper), lihat output streaming real-time.
5. Saat AI butuh konfirmasi (yes/no, pilih opsi, izin edit file / jalankan command),
   HP menerima **push notification** → buka app → tap **Approve / Deny**.
6. Sesi tetap hidup walau HP di-lock atau koneksi putus; bisa disambung lagi.

---

## 2. Kondisi Project Saat Ini (Hasil Analisis)

### 2.1 Arsitektur eksisting

```
HP (browser) ──HTTP/WS──> FastAPI (laptop Windows, port 8000)
                              ├── ProjectService  → scan C:\xampp\htdocs, D:\_2026
                              ├── CodexService    → jalankan `codex exec <prompt>` (one-shot)
                              └── WhisperService  → faster-whisper (bahasa Indonesia)
```

- **Backend:** FastAPI + uvicorn, single-file `backend/main.py` (215 baris) + 3 service.
- **Frontend:** satu file `frontend/index.html` (~800 baris), mobile-first, dark theme,
  terminal view dengan ansi_up, drawer project, input suara via MediaRecorder.
- **Voice:** faster-whisper lokal (CPU, int8), transkripsi bahasa Indonesia — sudah bagus.
- **Streaming output:** WebSocket `/ws/output` dengan callback broadcast — sudah bagus.

### 2.2 Yang sudah berfungsi baik (layak dipertahankan)

| Komponen | Catatan |
|---|---|
| Streaming output via WebSocket | Pola callback broadcast sudah benar |
| Voice-to-text Whisper lokal | Privasi terjaga, gratis, bahasa Indonesia |
| Project scan + custom path + mkdir | UX dasar pemilihan project sudah ada |
| Frontend mobile-first | Terminal + composer + drawer sudah nyaman dipakai |

### 2.3 Gap terhadap visi (ini inti masalahnya)

1. **Tidak interaktif.** `CodexService.send_input()` menjalankan
   `codex exec --skip-git-repo-check <prompt>` sebagai proses **one-shot** dengan
   `stdin=DEVNULL`. Artinya:
   - Tidak ada sesi percakapan berkelanjutan (tiap prompt = konteks baru).
   - **Tidak mungkin ada konfirmasi yes/no** — fitur yang paling diinginkan —
     karena stdin ditutup dan proses selesai sendiri.
2. **Satu mesin saja.** Backend diasumsikan jalan di laptop Windows
   (path hardcoded `C:\xampp\htdocs`, `D:\_2026`, `D:\whisper-models`, file `.bat`,
   `codex.cmd`). Tidak ada konsep multi-machine, padahal target: Windows 10 + Ubuntu.
3. **Codex saja.** Tidak ada dukungan Claude Code.
4. **Tidak ada notifikasi.** Tidak ada push notification saat AI butuh jawaban.
5. **Hanya LAN & tanpa keamanan.** Akses via `http://IP:8000`, **tanpa autentikasi
   sama sekali**, CORS `*`. Kalau ini diekspos ke internet, siapa pun bisa
   menjalankan perintah AI (yang bisa mengedit file & eksekusi shell) di mesin kamu.
   Ini blocker mutlak sebelum "akses dari mana saja".
6. **Satu sesi global.** Satu `CodexService` singleton — tidak bisa dua project /
   dua mesin berjalan paralel, dan tidak ada riwayat sesi.
7. **`PROJECT_ROOTS` hardcoded** di source code, bukan konfigurasi.

---

## 3. Persona & Use Case

**Persona:** developer solo (kamu) dengan 2 mesin kerja, sering mobile,
ingin "menyuruh" AI ngoding lalu hanya mengawasi & meng-approve dari HP.

**Use case utama:**

- UC-1: Pindah mesin dari HP (Windows ⇄ Ubuntu) tanpa setup ulang.
- UC-2: Buka/buat project apa pun di mesin terpilih.
- UC-3: Start sesi Claude Code **atau** Codex di project itu (pilih engine per sesi).
- UC-4: Prompt via teks atau suara, output streaming di HP.
- UC-5: AI minta konfirmasi → push notification ke HP → approve/deny dari notifikasi
  atau dari app.
- UC-6: Sesi berjalan lama (task besar) → HP boleh offline, saat buka lagi
  riwayat & status sesi tetap ada.

---

## 4. Kebutuhan Fungsional (Requirements)

### Must have (MVP)

- **FR-1 Multi-machine:** daftar mesin online/offline; pilih mesin aktif dari HP.
- **FR-2 Sesi interaktif:** sesi AI berkelanjutan (bukan one-shot) per project,
  dengan konteks percakapan yang menyambung.
- **FR-3 Dual engine:** pilih Claude Code atau Codex saat start sesi.
- **FR-4 Approval dari HP:** saat AI butuh izin (edit file, run command, pilihan
  yes/no), tampil kartu Approve/Deny di app + tombol jawab.
- **FR-5 Push notification:** notifikasi ke HP saat (a) AI butuh approval,
  (b) task selesai, (c) task error/berhenti.
- **FR-6 Autentikasi:** minimal token/passphrase; tidak ada endpoint tanpa auth.
- **FR-7 Akses dari luar rumah:** via jaringan privat (VPN mesh) atau tunnel.
- **FR-8 Project browser:** scan root folder (konfigurable per mesin), tambah path
  custom, buat folder baru — seperti sekarang, tapi per mesin.

### Should have

- **FR-9 Voice input** (sudah ada — dipertahankan, tapi jadikan opsional per mesin;
  PC Ubuntu mungkin tidak perlu load Whisper).
- **FR-10 Riwayat sesi:** daftar sesi per project, bisa resume
  (Claude Code punya `--resume/--continue` native).
- **FR-11 Status task:** indikator "AI sedang bekerja / menunggu jawaban / idle".
- **FR-12 Multi-sesi paralel:** ≥1 sesi per mesin (misal 2 project sekaligus).

### Nice to have (roadmap)

- FR-13 Diff viewer di HP sebelum approve edit file.
- FR-14 Git ops dari HP (status, diff, commit, push) tanpa lewat AI.
- FR-15 Quick actions / template prompt.
- FR-16 PWA installable + web-push (biar terasa seperti app native).
- FR-17 Mode "auto-approve" per sesi dengan scope terbatas (misal auto-yes untuk
  edit file, tetap tanya untuk shell command).

### Non-fungsional

- **NFR-1 Keamanan:** semua trafik terenkripsi; tidak ada port terbuka ke internet
  publik; secrets di `.env`/OS keyring.
- **NFR-2 Resiliensi:** WS putus → auto-reconnect + replay output yang terlewat;
  sesi AI tidak mati saat HP disconnect.
- **NFR-3 Latensi:** output stream terasa real-time (< 1 detik) di jaringan seluler.
- **NFR-4 Cross-platform agent:** satu codebase agent jalan di Windows 10 & Ubuntu.

---

## 5. Rekomendasi Arsitektur

### 5.1 Topologi: **Agent per mesin + jaringan privat (Tailscale)** — direkomendasikan

```
                    Tailscale mesh (WireGuard, gratis ≤3 user)
   HP (PWA) ────────────┬──────────────────────┬─────────────
                        │                      │
              Agent @ Laptop Win10      Agent @ PC Ubuntu
              (FastAPI + PTY/SDK)       (FastAPI + PTY/SDK)
```

- Tiap mesin menjalankan **Jarvis Agent** (evolusi backend sekarang).
- HP terhubung ke mesin mana pun lewat IP Tailscale (misal `laptop:8000`,
  `pc-ubuntu:8000`) — **aman dari mana saja tanpa port forwarding**, terenkripsi,
  dan tidak butuh server sewaan.
- Frontend menyimpan daftar mesin; "pindah mesin" = ganti base URL + WS.
- Alternatif topologi hub sentral (semua agent connect ke 1 server relay di cloud)
  lebih fleksibel untuk >2 user, tapi menambah komponen yang harus di-maintain —
  **tidak perlu untuk kasus solo developer**.

### 5.2 Sesi interaktif: **dua jalur, sesuai engine**

**Jalur A — Claude Code: pakai protokol terstruktur (rekomendasi utama).**
Claude Code mendukung mode headless terprogram
(`claude -p --output-format stream-json --input-format stream-json` /
Claude Agent SDK untuk Python). Keuntungannya besar:

- Event **permission request** datang sebagai JSON terstruktur → gampang dirender
  jadi kartu Approve/Deny di HP (bukan parsing teks terminal).
- Jawaban approve/deny dikirim balik sebagai JSON.
- `--resume <session-id>` native → riwayat sesi gratis.
- Tak perlu emulasi terminal; UI HP bisa berupa chat bubble yang rapi.
- Hook `Notification`/`Stop` di Claude Code bisa dipakai memicu push notification
  bahkan tanpa menunggu polling.

**Jalur B — Codex (dan CLI interaktif lain): pakai PTY.**
Jalankan CLI di pseudo-terminal (`pty` di Linux, `pywinpty`/ConPTY di Windows 10),
stream byte-nya ke HP, render dengan **xterm.js**. Input dari HP diteruskan ke PTY.
Yes/no prompt otomatis "kelihatan" karena ini terminal sungguhan. Deteksi
prompt konfirmasi (regex pola "y/n", "allow", "approve") memicu notifikasi.
Catatan: Codex juga punya `codex proto`/protocol mode yang bisa dieksplor belakangan
sebagai upgrade dari PTY.

> Strategi: **mulai dari PTY untuk keduanya** (paling cepat jalan, satu mekanisme
> untuk semua CLI), lalu upgrade Claude Code ke jalur terstruktur di fase 2 untuk
> UX approval yang jauh lebih baik.

### 5.3 Push notification: **Telegram bot** (keputusan final)

- Agent memakai Bot API dengan **long polling** (`getUpdates`) — tidak butuh
  webhook/port publik, aman di belakang NAT/Tailscale.
- Notifikasi: approval dibutuhkan / task selesai / task error, lengkap dengan
  nama mesin + project + cuplikan konteks.
- **Bonus besar dibanding ntfy:** pesan approval memakai **inline keyboard**
  (tombol ✅ Approve / ❌ Deny / 📱 Buka App) → kamu bisa menjawab konfirmasi
  **langsung dari notifikasi Telegram** tanpa buka app Jarvis. Jawaban diteruskan
  agent ke stdin PTY (`y`/`n` + Enter).
- Keamanan: bot hanya merespons `chat_id` milikmu (di-whitelist di config);
  token bot disimpan di `.env`.
- Satu bot dipakai kedua mesin; tiap pesan diberi prefix mesin
  (`[Win10]` / `[Ubuntu]`) agar jelas asalnya.

### 5.4 Penyimpanan & konfigurasi

- Config per mesin di `config.yaml` / `.env`: project roots, engine yang tersedia,
  token auth, topik ntfy — **hapus semua hardcoded path** dari source.
- SQLite kecil di tiap agent untuk riwayat sesi + buffer output (replay saat reconnect).

---

## 6. Desain Sistem Target (Ringkas)

```
┌─ HP: PWA (satu codebase) ─────────────────────────────┐
│ • Machine switcher (Win10 / Ubuntu, status online)    │
│ • Project browser per mesin                           │
│ • Session view: xterm.js ATAU chat view (Claude)      │
│ • Kartu Approve/Deny + tombol y/n/esc/ctrl-c          │
│ • Voice input (tetap)                                 │
└───────────────┬───────────────────────────────────────┘
                │ HTTPS/WSS via Tailscale + Bearer token
┌───────────────┴────────── Jarvis Agent (per mesin) ───┐
│ FastAPI                                               │
│ ├─ SessionManager: N sesi, tiap sesi = PTY proses     │
│ │   (claude / codex) ATAU Claude Agent SDK client     │
│ ├─ ProjectService (roots dari config, cross-platform) │
│ ├─ WhisperService (opsional per mesin)                │
│ ├─ Notifier → Telegram bot (approval / selesai /     │
│ │   error, inline button Approve/Deny)                │
│ └─ SQLite: sesi, output buffer, riwayat               │
└───────────────────────────────────────────────────────┘
```

---

## 7. Roadmap / Next Steps

### Fase 0 — Fondasi & keamanan ✅ SELESAI (14 Juli 2026)
1. ✅ Tailscale terinstall di ketiga perangkat (IP PC Ubuntu: `100.113.143.30`).
2. ✅ Telegram bot dibuat, token + chat_id tersimpan di `backend/.env`.
3. ✅ Autentikasi Bearer token di semua endpoint `/api/*` + WebSocket
   (query param `token`), pembanding `hmac.compare_digest`.
4. ✅ Config via `backend/config.py` + `.env` (PROJECT_ROOTS, Whisper opsional,
   nama mesin, override command engine) — cross-platform.

### Fase 1 — MVP interaktif ✅ SELESAI (14 Juli 2026, teruji di PC Ubuntu)
5. ✅ `services/session_manager.py`: sesi PTY multi-paralel (`ptyprocess` Linux,
   `pywinpty` Windows — jalur Windows belum diuji), engine claude/codex/shell,
   ring buffer 300K char dengan seq untuk replay saat reconnect.
6. ✅ Frontend xterm.js: terminal penuh, quick keys (Esc/Tab/panah/⏎/^C/y/n/1-3),
   composer + voice, auto-reconnect WS dengan replay `since=seq`.
7. ✅ Deteksi prompt approval (regex + idle 1.5s + dedup hash) →
   `services/telegram_service.py` kirim notif dengan inline button
   (1/2/3, y/n, Enter/Esc/↑/↓) yang diteruskan ke stdin PTY; reply pesan
   notifikasi = kirim teks bebas ke sesi; `/status` menampilkan daftar sesi.
8. ✅ Machine switcher di frontend (daftar mesin + token di localStorage).

**Hasil tes end-to-end di PC Ubuntu:** auth 401/200 benar, WS tanpa token
ditolak, echo via PTY OK, prompt `(y/n)` terdeteksi → Telegram terkirim,
input jawaban via HTTP & Telegram OK, exit frame OK, sesi `claude` sungguhan
tampil trust-prompt di TUI dan terdeteksi sebagai approval. Codex belum ada
di PATH PC Ubuntu (perlu `npm i -g @openai/codex` atau set `ENGINE_CODEX_CMD`).

### Fase 2 — UX approval kelas satu ✅ SELESAI (16 Juli 2026)
9. ✅ `services/chat_service.py` berbasis **Claude Agent SDK** (`claude-agent-sdk`,
   `ClaudeSDKClient` + callback `can_use_tool`): mode **Chat** di app —
   bubble percakapan, kartu tool, dan **kartu permission terstruktur**
   (✅ Izinkan / ❌ Tolak / ♾ Selalu) di app HP **dan** Telegram.
10. ✅ Riwayat sesi + resume: metadata & event tersimpan di SQLite
    (`backend/jarvis.db`), resume memakai `claude_session_id` native SDK —
    konteks percakapan menyambung setelah sesi ditutup/agent restart;
    WS auto-reconnect dengan replay `since=seq`.
11. ✅ PWA: `manifest.json` + `sw.js` (network-first) + ikon → installable
    di HP sebagai app.

**Hasil tes end-to-end (sesi Claude sungguhan):** prompt "buat file" →
permission request Bash muncul di app+Telegram → approve → file terbuat →
sesi ditutup → resume dari riwayat → replay 14 event → pertanyaan lanjutan
dijawab dengan konteks utuh, tool Read auto-allow (scope readonly) tanpa izin.

### Fase 3 — Kenyamanan ✅ SEBAGIAN BESAR SELESAI (16 Juli 2026)
12. ✅ Diff viewer di kartu permission & kartu tool untuk Edit/Write
    (blok merah/hijau old→new sebelum approve); ✅ git panel di drawer
    (status/diff/log/pull/push/commit via `/api/git`).
13. ✅ Multi-sesi paralel per mesin (PTY + chat) + selector sesi di topbar.
14. ✅ Template prompt (chips di atas composer, simpan/hapus);
    ✅ auto-approve ber-scope per sesi chat: readonly (default on) /
    edit file / bash / semua — toggle dari HP.
15. ⬜ (Opsional, belum) `codex proto` untuk approval terstruktur Codex.

---

## 8. Risiko & Mitigasi

| Risiko | Dampak | Mitigasi |
|---|---|---|
| Agent tereskpos internet tanpa auth | Remote code execution oleh orang asing | Tailscale-only + Bearer token (Fase 0, sebelum fitur lain) |
| PTY di Windows 10 rewel (ConPTY) | Sesi interaktif gagal di laptop | `pywinpty` sudah matang; fallback: jalankan agent Windows di WSL |
| Deteksi prompt yes/no via regex meleset | Notifikasi telat/salah | Heuristik ganda (regex + idle-timeout); jalur terstruktur Claude di Fase 2 menghilangkan masalah ini |
| Output ANSI berat di jaringan seluler | Lag | Buffer + batch per 50–100 ms sebelum kirim WS |
| Whisper berat di mesin kecil | Startup lambat | Jadikan opsional per mesin (config), lazy-load saat pertama dipakai |
| Sesi AI jalan lama tanpa pengawasan | Perubahan tak diinginkan | Default selalu minta approval; auto-approve hanya opt-in ber-scope |

---

## 9. Pertanyaan Terbuka — SUDAH DIJAWAB (14 Juli 2026)

1. **UI sesi:** ✅ **Terminal penuh (xterm.js) dulu**; chat view untuk Claude
   menyusul di Fase 2.
2. **Tailscale:** ✅ Dipakai. Sudah terinstall di laptop Win10 dan HP;
   **tinggal install di PC Ubuntu** (masuk Fase 0 langkah 1).
3. **Notifikasi:** ✅ **Telegram bot** — sekaligus jadi kanal approve/deny via
   inline button.
4. **Akun engine:** ✅ Claude Code & Codex sudah ter-setup di kedua mesin.
5. **Multi-user:** ✅ Single user (pemilik saja) → auth cukup satu Bearer token
   statis + whitelist chat_id Telegram; tidak perlu sistem login/multi-akun.

---

## 10. Keputusan Desain Final

- **D-1:** Topologi agent-per-mesin + Tailscale, tanpa server cloud sentral.
- **D-2:** Ganti `codex exec` one-shot → sesi PTY interaktif (blocker utama fitur approval).
- **D-3:** Claude Code akhirnya lewat jalur terstruktur (stream-json/Agent SDK),
  Codex tetap PTY dulu.
- **D-4:** Notifikasi & remote-approve via Telegram bot (long polling, inline
  keyboard, whitelist chat_id, satu bot untuk kedua mesin).
- **D-5:** Frontend tetap web (PWA) dengan terminal xterm.js, bukan app native.
- **D-6:** Single user: satu Bearer token statis di `.env`, tanpa sistem login.
