# Queue Monitor Indonesian TTS API

API Text-to-Speech (TTS) Bahasa Indonesia gratis, cepat (**100% in-memory tanpa menyimpan file ke disk**), dengan suara & intonasi seragam **ala pengumuman MRT Jakarta**, serta kompatibel langsung (*drop-in replacement*) dengan **SoundOfText** untuk sistem antrian seperti `queue_server_21`.

## Fitur Utama

- **Gratis Tanpa Token / API Key**: Memakai `edge-tts` (`id-ID-GadisNeural`, `rate="-8%"`, `pitch="+2Hz"`).
- **Intonasi & Suara Seragam**: Kirim nama lengkap atau kalimat antrian utuh dalam 1 request agar tidak terdengar patah-patah.
- **Tanpa Simpan File di Disk**: Audio di-stream langsung di RAM. Untuk endpoint 2-langkah `/sounds` → `/{id}.mp3`, file ditahan sementara di RAM (`EphemeralAudioStore`, maks 100 item, TTL 120 detik) lalu otomatis terhapus.
- **Kompatibel `queue_server_21`**: Mendukung `POST /sounds`, `GET /sounds/{id}.mp3`, `GET /{id}.mp3`, serta otomatis menangani double slash (`//`).

---

## Cara Mengaktifkan & Menjalankan di VPS (24 Jam)

### Opsi 1: Menggunakan Docker (Paling Praktis)

Pastikan Docker sudah terpasang di VPS Anda, lalu jalankan:

```bash
git clone https://github.com/excaandita/tts.git
cd tts

docker build -t queue-tts-api .
docker run -d \
  --name queue-tts-api \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  queue-tts-api
```

> **Catatan port:** Gunakan `-p 127.0.0.1:8000:8000` jika aplikasi antrian berada di VPS yang sama (lebih aman karena tidak terbuka ke publik). Jika diakses dari server lain, gunakan `-p 8000:8000` dan batasi akses port `8000` lewat firewall (`ufw`) hanya untuk IP server aplikasi antrian Anda.

Cek apakah container berjalan:

```bash
curl http://127.0.0.1:8000/health
```

---

### Opsi 2: Tanpa Docker (`uv` + `systemd` di Ubuntu/Debian VPS)

1. **Install `uv` dan (opsional) `ffmpeg`:**
   ```bash
   sudo apt update && sudo apt install -y curl git ffmpeg
   curl -LsSf https://astral.sh/uv/install.sh | sh
   source ~/.bashrc
   ```

2. **Clone repo dan install dependensi:**
   ```bash
   git clone https://github.com/excaandita/tts.git /opt/tts
   cd /opt/tts
   uv sync --frozen --no-dev
   ```

3. **Tes jalan manual:**
   ```bash
   uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```

4. **Aktifkan sebagai service `systemd` agar jalan 24 jam & auto-start saat reboot:**

   Buat file `/etc/systemd/system/queue-tts.service`:
   ```ini
   [Unit]
   Description=Queue Monitor Indonesian TTS API
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=simple
   User=root
   WorkingDirectory=/opt/tts
   Environment="TTS_DEFAULT_VOICE=mrt_female"
   Environment="TTS_DEFAULT_CHIME=false"
   ExecStart=/root/.local/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
   Restart=always
   RestartSec=3

   [Install]
   WantedBy=multi-user.target
   ```
   *(Sesuaikan `User` dan path `/root/.local/bin/uv` dengan output `which uv` di VPS Anda.)*

   Aktifkan dan jalankan service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now queue-tts
   sudo systemctl status queue-tts
   ```

   Melihat log real-time:
   ```bash
   sudo journalctl -u queue-tts -f
   ```

---

## Cara Menggunakan

### 1. Integrasi ke `queue_server_21` (Tanpa Ubah Kode Scala)

Cukup ubah 2 baris di `conf/application.conf`:

```hocon
  urlGenSound = "http://127.0.0.1:8000/sounds"
  urlDownSound = "http://127.0.0.1:8000/sounds"
```
*(Ganti `127.0.0.1:8000` dengan IP/domain VPS bila beda server.)*

Alur yang berjalan otomatis:
1. Aplikasi memanggil `POST /sounds` → menerima `{"success": true, "id": "tts_..."}`
2. Aplikasi memanggil `GET /sounds/tts_....mp3` → menerima file `.mp3` dari RAM lalu menyimpannya di `app.audioDir` milik aplikasi antrian Anda.

### 2. Pemakaian Langsung via HTTP (`POST /tts`)

Mengembalikan file audio langsung di response body (1 request):

```bash
curl -X POST http://127.0.0.1:8000/tts \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Nomor antrian A kosong satu, atas nama Alex Marquez Muldoko Satyo, silakan menuju loket dua."
  }' \
  --output antrian.mp3
```

Parameter opsional pada body JSON `POST /tts`:
- `voice`: `"mrt_female"` (default, `id-ID-GadisNeural` ala MRT Jakarta), `"mrt_male"` (`id-ID-ArdiNeural`), atau `"offline"` (khusus macOS `Damayanti`).
- `chime`: `false` (default) atau `true` untuk menambahkan bunyi bel pembuka 3 nada.
- `format`: `"mp3"` (default) atau `"wav"`. *(Jika `chime: true` tanpa `ffmpeg`, otomatis menghasilkan `"wav"`.)*

### 3. Cek Status Server (`GET /health`)

```bash
curl http://127.0.0.1:8000/health
```

### 4. Konfigurasi Environment Variable (Opsional)

- `TTS_DEFAULT_VOICE`: `mrt_female` (default), `mrt_male`, atau `offline`
- `TTS_DEFAULT_CHIME`: `false` (default) atau `true` *(untuk endpoint `/sounds`, butuh `ffmpeg` agar output tetap `.mp3`)*
- `CORS_ALLOW_ORIGINS`: `*` (default) atau daftar origin dipisah koma, mis. `https://antrian.rsyasyfin.com,https://admin.rsyasyfin.com`
- `RATE_LIMIT_PER_MINUTE`: `120` (default). Isi `0` untuk mematikan. Batas dihitung per IP klien.
- `TRUST_PROXY_HEADERS`: `true` (default). Biarkan aktif bila berjalan di belakang nginx/Caddy agar IP asli dibaca dari `X-Forwarded-For`.

Contoh menjalankan dengan pembatasan lebih ketat:

```bash
docker run -d \
  --name queue-tts-api \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e RATE_LIMIT_PER_MINUTE=60 \
  -e CORS_ALLOW_ORIGINS=https://antrian.rsyasyfin.com \
  queue-tts-api
```

Bila batas terlampaui, API membalas `429` dengan header `Retry-After` (detik). Endpoint `/health`, `/docs`, dan `/openapi.json` dikecualikan agar monitoring tidak ikut terblokir.

---

## Menjalankan Tes

```bash
uv run pytest -v
```
