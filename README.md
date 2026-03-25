# 🦆 Kwek — Family AI Bot

Asisten keluarga berbasis Telegram + Claude AI.

## Fitur
- 📸 Buat cerita dari foto keluarga dengan AI interview
- 💬 Asisten umum — tanya apapun
- 🎬 Claude Vision analisis foto otomatis

## Deploy ke Railway

### 1. Upload ke GitHub
- Buat repository baru di github.com (nama: kwek-bot)
- Upload 3 file ini: bot.py, requirements.txt, Procfile

### 2. Deploy di Railway
- Buka railway.app
- Klik "New Project" → "Deploy from GitHub repo"
- Pilih repository kwek-bot
- Klik "Deploy"

### 3. Isi Environment Variables
Di Railway → Settings → Variables, tambahkan:

```
TELEGRAM_TOKEN = token dari BotFather
CLAUDE_API_KEY = sk-ant-... dari Anthropic Console
```

### 4. Selesai!
Bot langsung aktif. Test di Telegram dengan /start

## Commands
- /start - Sapa Kwek
- /help - Panduan
- /cerita - Mulai sesi foto
- /selesai - Foto sudah semua
- /batal - Batalkan sesi
