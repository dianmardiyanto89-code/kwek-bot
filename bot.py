import os
import logging
import base64
import httpx
import json
import re
from datetime import datetime, timezone, time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── ENV VARIABLES ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "").strip()
CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY", "").strip()
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY    = os.environ.get("SUPABASE_KEY", "").strip()
GROUP_CHAT_ID   = int(os.environ.get("GROUP_CHAT_ID", "0"))
CLAUDE_API_URL  = "https://api.anthropic.com/v1/messages"
TIMEZONE_OFFSET = 7  # WIB

if not TELEGRAM_TOKEN: raise ValueError("TELEGRAM_TOKEN tidak ditemukan!")
if not CLAUDE_API_KEY:  raise ValueError("CLAUDE_API_KEY tidak ditemukan!")
if not SUPABASE_URL:    raise ValueError("SUPABASE_URL tidak ditemukan!")
if not SUPABASE_KEY:    raise ValueError("SUPABASE_KEY tidak ditemukan!")

# ─── MOOD CHANNELS ───────────────────────────────────────────────────────────
CHANNELS = {
    # Mood harian
    "biasa":     {"label": "😊 Biasa Aja",      "group": "mood"},
    "sedih":     {"label": "😔 Lagi Berat",      "group": "mood"},
    "semangat":  {"label": "🤩 Lagi Semangat",   "group": "mood"},
    "capek":     {"label": "😤 Lagi Capek",       "group": "mood"},
    # Mau apa
    "soleh":     {"label": "🕌 Mau Soleh",        "group": "mau"},
    "lucu":      {"label": "😂 Mau Ketawa",       "group": "mau"},
    "funfact":   {"label": "🤓 Fun Fact",          "group": "mau"},
    "motivasi":  {"label": "💪 Butuh Semangat",   "group": "mau"},
    # Anak-anak
    "belajar":   {"label": "📚 Mau Belajar",      "group": "anak"},
    "main":      {"label": "🎮 Mau Main",          "group": "anak"},
    "tidur":     {"label": "🌙 Mau Tidur",         "group": "anak"},
    # Kontekstual
    "kejutan":   {"label": "🔮 Kejutan Kwek",     "group": "auto"},
}

CHANNEL_KEYBOARD = [
    [InlineKeyboardButton("😊 Biasa Aja", callback_data="ch_biasa"),
     InlineKeyboardButton("😔 Lagi Berat", callback_data="ch_sedih")],
    [InlineKeyboardButton("🤩 Semangat", callback_data="ch_semangat"),
     InlineKeyboardButton("😤 Lagi Capek", callback_data="ch_capek")],
    [InlineKeyboardButton("🕌 Mau Soleh", callback_data="ch_soleh"),
     InlineKeyboardButton("😂 Mau Ketawa", callback_data="ch_lucu")],
    [InlineKeyboardButton("🤓 Fun Fact", callback_data="ch_funfact"),
     InlineKeyboardButton("💪 Butuh Semangat", callback_data="ch_motivasi")],
    [InlineKeyboardButton("📚 Mau Belajar", callback_data="ch_belajar"),
     InlineKeyboardButton("🎮 Mau Main", callback_data="ch_main")],
    [InlineKeyboardButton("🌙 Mau Tidur", callback_data="ch_tidur"),
     InlineKeyboardButton("🔮 Kejutan Kwek", callback_data="ch_kejutan")],
]

# ─── TRIGGER DETECTION ───────────────────────────────────────────────────────
FINANCE_EXPENSE_WORDS = {"catat", "cata", "catet", "keluar", "keluarin", "bayar", "beli"}
FINANCE_INCOME_WORDS  = {"masuk", "dapat", "terima", "gaji", "gajian", "pemasukan"}
REMINDER_WORDS        = {"ingatkan", "ingatin", "reminder", "remind", "inget", "pengingat"}
TASK_WORDS            = {"todo", "tugas", "kerjain", "task"}
JOURNAL_WORDS         = {"kabar", "curhat", "cerita", "hari", "jurnal"}

FINANCE_SYMBOL_RE = re.compile(r'^([+\-])\s*(\d[\d.,]*[kKmMrRbB]*)\s+(.+)$')

def detect_trigger(text: str) -> str:
    t          = text.strip().lower()
    first_word = t.split()[0] if t.split() else ""
    if FINANCE_SYMBOL_RE.match(text.strip()):
        return "income" if text.strip()[0] == "+" else "expense"
    if first_word in FINANCE_INCOME_WORDS:  return "income"
    if first_word in FINANCE_EXPENSE_WORDS: return "expense"
    for word in FINANCE_EXPENSE_WORDS | FINANCE_INCOME_WORDS:
        if _edit_distance(first_word, word) == 1 and len(first_word) >= 4:
            return "income" if word in FINANCE_INCOME_WORDS else "expense"
    if first_word in REMINDER_WORDS or any(w in t for w in REMINDER_WORDS):
        return "reminder"
    if first_word in TASK_WORDS: return "task"
    return "chat"

def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2: return 99
    if a == b: return 0
    dp = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        new_dp = [i + 1]
        for j, cb in enumerate(b):
            new_dp.append(min(dp[j] + (0 if ca == cb else 1), dp[j+1]+1, new_dp[j]+1))
        dp = new_dp
    return dp[len(b)]

def parse_amount_from_text(text: str) -> tuple:
    m = FINANCE_SYMBOL_RE.match(text.strip())
    if m: return _parse_number(m.group(2)), m.group(3).strip()
    num_re = re.search(r'(\d[\d.,]*\s*[kKmMrRbBjJ]{0,3})', text)
    if num_re:
        amount = _parse_number(num_re.group(0))
        desc   = re.sub(r'\d[\d.,]*\s*[kKmMrRbBjJ]{0,3}', '', text).strip()
        desc   = re.sub(r'\s+', ' ', desc).strip()
        words  = desc.split()
        if words and words[0].lower() in FINANCE_EXPENSE_WORDS | FINANCE_INCOME_WORDS:
            desc = ' '.join(words[1:])
        return amount, desc or text
    return 0, text

def _parse_number(s: str) -> int:
    s = s.strip().lower().replace(',', '').replace(' ', '')
    mult = 1
    if re.search(r'(rb|ribu|k)$', s):  mult = 1000;      s = re.sub(r'(rb|ribu|k)$', '', s)
    elif re.search(r'(jt|juta|j)$', s): mult = 1_000_000; s = re.sub(r'(jt|juta|j)$', '', s)
    elif re.search(r'(m)$', s):          mult = 1_000_000; s = re.sub(r'm$', '', s)
    try:    return abs(int(float(s) * mult))
    except: return 0

# ─── SUPABASE CLIENT ─────────────────────────────────────────────────────────
class SupabaseClient:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json", "Prefer": "return=representation"
        }
        self.storage_headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    async def insert(self, table: str, data: dict) -> dict | None:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.url}/rest/v1/{table}", headers=self.headers, json=data)
            if r.status_code in (200, 201):
                res = r.json()
                return res[0] if isinstance(res, list) and res else res
            logger.error(f"insert {table} {r.status_code}: {r.text}")
            return None

    async def select(self, table: str, query: str = "") -> list:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{self.url}/rest/v1/{table}?{query}", headers=self.headers)
            return r.json() if r.status_code == 200 else []

    async def update(self, table: str, match: str, data: dict) -> bool:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.patch(f"{self.url}/rest/v1/{table}?{match}", headers=self.headers, json=data)
            return r.status_code in (200, 204)

    async def upload_photo(self, bucket: str, path: str, image_bytes: bytes) -> str | None:
        headers = {**self.storage_headers, "Content-Type": "image/jpeg", "x-upsert": "true"}
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{self.url}/storage/v1/object/{bucket}/{path}", headers=headers, content=image_bytes)
            if r.status_code in (200, 201):
                return f"{self.url}/storage/v1/object/{bucket}/{path}"
            logger.error(f"upload {r.status_code}: {r.text}")
            return None

db = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)

# ─── SESSION ─────────────────────────────────────────────────────────────────
sessions = {}

def get_sess(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = {
            "mode": "idle", "photos": [], "photo_bytes": [],
            "photo_urls": [], "history": [], "q_count": 0, "general": [],
            "awaiting_journal": False, "story_mode": "interview"
        }
    return sessions[chat_id]

def reset_sess(chat_id):
    sessions[chat_id] = {
        "mode": "idle", "photos": [], "photo_bytes": [], "photo_urls": [],
        "history": [], "q_count": 0, "general": [], "awaiting_journal": False,
        "story_mode": "interview"
    }

# ─── CLAUDE API ──────────────────────────────────────────────────────────────
SYSTEM_KWEK = """Kamu adalah Kwek, asisten keluarga yang ceria, hangat, cerdas, dan sedikit lucu.
Berbicara Bahasa Indonesia yang natural dan santai. Gunakan emoji secukupnya.

Kemampuan:
1. SUTRADARA CERITA: Interview keluarga berdasarkan foto, lalu buatkan cerita indah
2. ASISTEN UMUM: Jawab pertanyaan apapun dengan ramah
3. WARTAWAN KELUARGA: Tanya kabar harian setiap anggota dengan hangat
4. MOOD CHANNEL: Generate konten sesuai channel yang dipilih

Saat interview foto:
- Analisis dengan empati dan detail
- Pertanyaan personal, spesifik, hangat
- Maksimal 4 pertanyaan sebelum buat cerita"""

SYSTEM_STORY = """Kamu adalah penulis cerita keluarga yang puitis dan berbakat.
Tulis cerita berdasarkan foto dan hasil interview.
Gunakan Bahasa Indonesia yang indah, personal, menyentuh tapi tidak berlebihan."""

SYSTEM_FINANCE_PHOTO = """Kamu adalah parser keuangan untuk foto struk/nota belanja.
Balas HANYA dengan JSON valid, tanpa teks lain.
{"is_transaction":true,"amount":321200,"type":"expense","description":"belanja Indomaret","category":"Belanja","merchant":"Indomaret","notes":null}
ATURAN: amount = TOTAL BAYAR FINAL setelah diskon, selalu positif.
Kategori: Makanan & Minuman, Transportasi, Belanja, Kesehatan, Pendidikan, Hiburan, Tagihan & Utilitas, Tabungan & Investasi, Pemasukan, Lainnya"""

SYSTEM_FINANCE_TEXT = """Kamu adalah parser keuangan untuk teks chat.
Balas HANYA dengan JSON valid, tanpa teks lain.
{"amount":15000,"description":"beli baso","category":"Makanan & Minuman","merchant":null,"notes":null}
ATURAN: amount selalu positif. 15rb=15000, 5jt=5000000.
Kategori: Makanan & Minuman, Transportasi, Belanja, Kesehatan, Pendidikan, Hiburan, Tagihan & Utilitas, Tabungan & Investasi, Pemasukan, Lainnya"""

SYSTEM_REMINDER = """Kamu adalah parser pengingat keluarga.
Balas HANYA dengan JSON valid, tanpa teks lain.
{"title":"Bayar listrik","notes":"Jatuh tempo tanggal 20","due_date":"2026-04-20"}
due_date format YYYY-MM-DD jika disebutkan, null jika tidak ada."""

SYSTEM_TASK = """Kamu adalah parser tugas keluarga.
Balas HANYA dengan JSON valid, tanpa teks lain.
{"title":"Beli deterjen","notes":"Merk Rinso ukuran besar","priority":"medium"}
priority: high=urgent, medium=default, low=santai."""

SYSTEM_JOURNAL_CURATOR = """Kamu adalah kurator jurnal keluarga yang hangat dan puitis.
Tugasmu: ubah curahan hati mentah menjadi catatan jurnal yang indah, personal, dan bermakna.
Konteks tetap utuh, hanya bahasa yang diperhalus dan diperkaya.
Panjang: 2-3 kalimat. Bahasa Indonesia yang hangat dan mengalir.
Jangan tambahkan informasi yang tidak ada. Jangan berlebihan."""

async def call_claude(messages: list, system: str, images: list = None) -> str:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    if images:
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in images]
        last    = messages[-1]["content"] if messages else "Analisis foto ini."
        content.append({"type": "text", "text": last})
        api_messages = messages[:-1] + [{"role": "user", "content": content}]
    else:
        api_messages = messages
    payload = {"model": "claude-sonnet-4-20250514", "max_tokens": 1000, "system": system, "messages": api_messages}
    async with httpx.AsyncClient(timeout=60) as c:
        resp = await c.post(CLAUDE_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

async def call_claude_json(messages: list, system: str, images: list = None) -> dict | None:
    try:
        raw   = await call_claude(messages, system, images)
        clean = raw.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)
        # Kalau Claude return array, ambil element pertama
        if isinstance(result, list):
            result = result[0] if result else None
        return result if isinstance(result, dict) else None
    except Exception as e:
        logger.error(f"call_claude_json: {e}")
        return None

# ─── MOOD CHANNEL CONTENT GENERATOR ─────────────────────────────────────────
CHANNEL_PROMPTS = {
    "biasa": "Buat konten hangat dan positif untuk memulai hari keluarga Indonesia. Mix antara motivasi ringan, hal menarik, atau pengingat sederhana. 2-3 kalimat.",
    "sedih": "Seseorang sedang merasa berat hari ini. Buat pesan penguat yang tulus, hangat, islami ringan, dan tidak menggurui. 2-3 kalimat penuh empati.",
    "semangat": "Seseorang sedang semangat! Buat konten yang match dengan energi positif mereka — tantangan kecil, target, atau apresiasi. 2-3 kalimat.",
    "capek": "Seseorang sedang capek. Buat pesan yang mengapresiasi kerja keras mereka, reminder untuk istirahat, dan hal sederhana yang menyenangkan. 2-3 kalimat.",
    "soleh": "Buat konten islami yang hangat untuk keluarga Muslim Indonesia. Bisa ayat pendek dengan terjemahan, hadits ringan, amalan harian, atau kisah singkat sahabat. Sertakan sumber jika ayat/hadits. 3-4 kalimat.",
    "lucu": "Buat humor halal yang lucu untuk keluarga Indonesia. Bisa jokes keluarga, humor situasi sehari-hari, atau fakta absurd yang menggelikan. Harus benar-benar lucu dan tidak menyinggung. 2-3 kalimat.",
    "funfact": "Buat 1 fakta unik dan menarik tentang sains, sejarah, alam, atau teknologi yang relevan untuk keluarga Indonesia. Mulai dengan 'Tahukah kamu?' Fakta harus akurat dan mengejutkan. 2-3 kalimat.",
    "motivasi": "Buat konten motivasi yang genuine dan tidak klise untuk keluarga Indonesia. Bisa kisah singkat inspiratif, quotes bermakna, atau pengingat pencapaian kecil. 2-3 kalimat.",
    "belajar": "Buat konten edukatif yang fun untuk anak SD. Bisa teka-teki, fakta pelajaran sekolah yang menarik, atau quiz singkat. Bahasa sederhana dan menyenangkan. 2-3 kalimat.",
    "main": "Buat tantangan permainan seru yang bisa dimainkan keluarga. Bisa tebak-tebakan, word game, atau tantangan lucu yang bisa dilakukan di rumah. 2-3 kalimat.",
    "tidur": "Buat pengantar tidur yang tenang dan hangat. Bisa pembuka dongeng singkat, doa malam dalam Bahasa Indonesia, atau kata-kata malam yang menenangkan untuk anak. 2-3 kalimat.",
    "kejutan": "Pilih sendiri konten terbaik untuk keluarga Indonesia hari ini — bisa apapun: fakta menarik, humor, motivasi, atau konten islami. Buat sesuatu yang terasa spesial dan tak terduga. 2-3 kalimat.",
}

async def generate_channel_content(channel: str, member_name: str) -> str:
    prompt_base = CHANNEL_PROMPTS.get(channel, CHANNEL_PROMPTS["kejutan"])
    prompt = f"Untuk: {member_name}\n{prompt_base}\nPersonalisasi dengan menyebut nama {member_name} jika natural."
    try:
        result = await call_claude(
            messages=[{"role": "user", "content": prompt}],
            system="Kamu adalah Kwek, asisten keluarga yang hangat dan cerdas. Buat konten yang personal, genuine, dan tidak generik."
        )
        return result
    except Exception as e:
        logger.error(f"generate_channel_content: {e}")
        return "Kwek sedang menyiapkan sesuatu spesial untukmu hari ini! 🦆✨"

async def curate_journal(raw_text: str, member_name: str) -> str:
    try:
        result = await call_claude(
            messages=[{"role": "user", "content": f"Nama: {member_name}\nCurahan hati: {raw_text}"}],
            system=SYSTEM_JOURNAL_CURATOR
        )
        return result
    except Exception as e:
        logger.error(f"curate_journal: {e}")
        return raw_text

# ─── DB HELPERS ──────────────────────────────────────────────────────────────
async def ensure_member(telegram_id: int, username: str = None, full_name: str = None) -> dict | None:
    existing = await db.select("family_members", f"telegram_id=eq.{telegram_id}")
    if existing: return existing[0]
    return await db.insert("family_members", {
        "telegram_id": telegram_id,
        "username":    username or f"user_{telegram_id}",
        "full_name":   full_name or "Anggota Keluarga",
        "role":        "member"
    })

async def upload_photo(image_bytes: bytes, folder: str, member_id: str) -> str | None:
    ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    path = f"{folder}/{member_id}/{ts}.jpg"
    return await db.upload_photo("photos", path, image_bytes)

async def save_finance(member_id: str, parsed: dict, tx_type: str, photo_url: str = None) -> dict | None:
    cats   = await db.select("finance_categories", f"name=eq.{parsed.get('category','Lainnya')}")
    cat_id = cats[0]["id"] if cats else None
    return await db.insert("finance_transactions", {
        "recorded_by": member_id, "type": tx_type,
        "amount":      abs(parsed.get("amount", 0)),
        "description": parsed.get("description", ""),
        "category_id": cat_id, "merchant": parsed.get("merchant"),
        "notes":       parsed.get("notes"), "receipt_url": photo_url,
        "source":      "photo" if photo_url else "bot"
    })

async def save_story_db(member_id: str, story_text: str, photo_urls: list) -> dict | None:
    title = "Cerita Keluarga"
    for line in story_text.split("\n"):
        line = line.strip().replace("*", "").replace("_", "")
        if line and 5 < len(line) < 80 and not line.startswith("#"):
            title = line; break
    try:
        story = await db.insert("stories", {
            "author_id":   member_id,
            "title":       title,
            "story_text":  story_text,
            "photo_count": len(photo_urls),
            "mood":        "happy",
            "tags":        ["keluarga", "momen"]
        })
        if not story:
            logger.error(f"save_story_db: insert stories gagal, member_id={member_id}")
            return None
        logger.info(f"save_story_db: story tersimpan id={story.get('id')}")
        if photo_urls:
            for i, url in enumerate(photo_urls):
                res = await db.insert("story_photos", {
                    "story_id":    story["id"],
                    "photo_url":   url,
                    "order_index": i+1
                })
                if not res:
                    logger.error(f"save_story_db: gagal simpan story_photo url={url}")
                else:
                    logger.info(f"save_story_db: photo {i+1} tersimpan")
        return story
    except Exception as e:
        logger.error(f"save_story_db exception: {e}")
        return None

async def save_journal(member_id: str, raw_text: str, curated_text: str, mood_score: int, channel: str) -> dict | None:
    return await db.insert("daily_journal", {
        "member_id":    member_id,
        "journal_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "raw_text":     raw_text,
        "curated_text": curated_text,
        "mood_score":   mood_score,
        "mood_channel": channel,
        "is_curated":   True
    })

async def save_display_queue(member_id: str, channel: str, ai_context: str, content_type: str = "channel") -> dict | None:
    return await db.insert("display_queue", {
        "member_id":    member_id,
        "display_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "content_type": content_type,
        "channel_type": channel,
        "ai_context":   ai_context,
        "is_shown":     False
    })

async def save_reminder_db(member_id: str, parsed: dict) -> dict | None:
    data = {"target_member_id": member_id, "title": parsed.get("title", "Pengingat"),
            "message": parsed.get("notes"), "is_active": True, "created_by": member_id, "reminder_type": "manual"}
    if parsed.get("due_date"):
        data["trigger_time"] = parsed["due_date"] + "T08:00:00+07:00"
    return await db.insert("reminders", data)

async def save_task_db(member_id: str, parsed: dict) -> dict | None:
    return await db.insert("tasks", {
        "assigned_to": member_id, "created_by": member_id,
        "title":       parsed.get("title", "Tugas baru"),
        "description": parsed.get("notes"),
        "priority":    parsed.get("priority", "medium"),
        "status":      "pending", "source": "bot"
    })

async def log_activity(member_id: str, action: str, details: dict = None):
    await db.insert("bot_activity_log", {
        "action_type":   action,
        "action_detail": details or {}
    })

# ─── REPLY HELPERS ───────────────────────────────────────────────────────────
async def reply_finance(update: Update, parsed: dict, tx_type: str, extra: str = ""):
    amt   = abs(parsed.get("amount", 0))
    desc  = parsed.get("description", "")
    cat   = parsed.get("category", "Lainnya")
    merch = parsed.get("merchant")
    emoji = "Pemasukan" if tx_type == "income" else "Pengeluaran"
    m_line = f"\nToko: {merch}" if merch else ""
    await update.message.reply_text(
        f"*{emoji} Tercatat!*\n\n{desc}: Rp {amt:,.0f}{m_line}\nKategori: {cat}{extra}",
        parse_mode="Markdown"
    )

# ─── SCHEDULER — BOT WARTAWAN ────────────────────────────────────────────────
async def job_pagi(context):
    """06.30 WIB — Sapaan pagi + pilih mood channel"""
    if not GROUP_CHAT_ID: return
    members = await db.select("family_members", "select=id,full_name,telegram_id&role=neq.admin")
    if not members: return

    now   = datetime.now(timezone.utc)
    hari  = ["Senin","Selasa","Rabu","Kamis","Jumat","Sabtu","Minggu"]
    bulan = ["Jan","Feb","Mar","Apr","Mei","Jun","Jul","Agt","Sep","Okt","Nov","Des"]
    tgl   = f"{hari[now.weekday()]}, {now.day} {bulan[now.month-1]} {now.year}"

    # Tentukan siapa yang dapat giliran hari ini (round-robin)
    day_idx   = now.timetuple().tm_yday
    member    = members[day_idx % len(members)]
    name      = member.get("full_name", "Teman")
    tg_id     = member.get("telegram_id")
    mention   = f"[{name}](tg://user?id={tg_id})" if tg_id else name

    keyboard = InlineKeyboardMarkup(CHANNEL_KEYBOARD)
    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=(
            f"🌅 *Selamat pagi, {mention}!*\n"
            f"_{tgl}_\n\n"
            f"Hari ini Kwek mau temani kamu dengan apa?\n"
            f"Pilih channel-mu hari ini:"
        ),
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def job_sore(context):
    """17.00 WIB — Bot wartawan tanya kabar"""
    if not GROUP_CHAT_ID: return
    members = await db.select("family_members", "select=id,full_name,telegram_id")
    if not members: return

    now      = datetime.now(timezone.utc)
    day_idx  = now.timetuple().tm_yday
    member   = members[day_idx % len(members)]
    name     = member.get("full_name", "Teman")
    tg_id    = member.get("telegram_id")
    mention  = f"[{name}](tg://user?id={tg_id})" if tg_id else name

    # Simpan di session bahwa kita menunggu jurnal dari member ini
    sessions[f"wartawan_{member['id']}"] = {"waiting": True, "member": member}

    pertanyaan = [
        f"Hei {mention}! Sore sudah tiba — hari ini gimana? Ada momen seru, lucu, atau mengharukan? Cerita dong ke Kwek! 📝",
        f"Kwek kangen kabar {mention}! Hari ini ada yang berkesan? Hal kecil sekalipun boleh banget diceritain 🦆",
        f"Sore {mention}! Kalau hari ini jadi 1 kata, kira-kira kata apa? Dan kenapa? Kwek penasaran! ✨",
        f"Hai {mention}, hari ini ada kisah yang mau dibagi ke keluarga? Kwek siap dengerin! 💛",
    ]
    import random
    teks = random.choice(pertanyaan)

    await context.bot.send_message(
        chat_id=GROUP_CHAT_ID,
        text=teks,
        parse_mode="Markdown"
    )

async def job_malam(context):
    """20.30 WIB — Reminder malam jika belum isi jurnal"""
    if not GROUP_CHAT_ID: return

    now     = datetime.now(timezone.utc)
    today   = now.strftime("%Y-%m-%d")
    members = await db.select("family_members", "select=id,full_name,telegram_id")

    for member in members:
        # Cek apakah sudah ada jurnal hari ini
        journals = await db.select("daily_journal",
            f"member_id=eq.{member['id']}&journal_date=eq.{today}&select=id")
        if journals: continue  # Sudah isi, skip

        name    = member.get("full_name", "Teman")
        tg_id   = member.get("telegram_id")
        mention = f"[{name}](tg://user?id={tg_id})" if tg_id else name

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=(
                f"🌙 {mention}, jurnal hari ini belum terisi nih!\n"
                f"Mau cerita apa saja — sekecil apapun — biar Kwek catat jadi kenangan keluarga ya 💛\n\n"
                f"Cukup balas pesan ini dengan cerita singkatmu hari ini."
            ),
            parse_mode="Markdown"
        )

# ─── CALLBACK HANDLER — MOOD CHANNEL ─────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data
    user  = query.from_user

    # ── Foto pilihan handler ──
    if data.startswith("foto_"):
        chat_id = query.message.chat_id
        sess    = get_sess(chat_id)
        member  = await ensure_member(user.id, user.username, user.full_name)
        member_id = member["id"] if member else "unknown"
        b64       = sess.pop("pending_b64", None)
        raw_bytes = sess.pop("pending_bytes", None)

        await query.edit_message_reply_markup(reply_markup=None)

        if data == "foto_ceritacepat":
            if b64 and raw_bytes:
                sess["mode"] = "collecting"
                sess["story_mode"] = "quick"
                sess["photos"].append(b64)
                sess["photo_bytes"].append(raw_bytes)
                await ctx.bot.send_message(chat_id,
                    "⚡ *Mode Cerita Cepat!*\n\nFoto masuk (1/8).\nKirim foto lagi atau /selesai lalu tulis ceritamu!",
                    parse_mode="Markdown")
        elif data == "foto_ceritalengkap":
            if b64 and raw_bytes:
                sess["mode"] = "collecting"
                sess["story_mode"] = "interview"
                sess["photos"].append(b64)
                sess["photo_bytes"].append(raw_bytes)
                await ctx.bot.send_message(chat_id,
                    "🎬 *Mode Cerita Lengkap!*\n\nFoto masuk (1/8).\nKirim foto lagi atau /selesai untuk mulai wawancara!",
                    parse_mode="Markdown")
        elif data == "foto_reminder":
            if raw_bytes:
                photo_url = await upload_photo(raw_bytes, "misc", member_id)
                await db.insert("bot_activity_log", {
                    "action_type": "photo_reminder",
                    "action_detail": {"photo_url": photo_url}
                })
                await ctx.bot.send_message(chat_id, "📌 Foto tersimpan sebagai pengingat!")
        elif data == "foto_log":
            if raw_bytes:
                photo_url = await upload_photo(raw_bytes, "misc", member_id)
                await db.insert("bot_activity_log", {
                    "action_type": "photo_log",
                    "action_detail": {"photo_url": photo_url}
                })
                await ctx.bot.send_message(chat_id, "📋 Foto tersimpan sebagai log aktivitas!")
        return

    if not data.startswith("ch_"): return

    channel = data[3:]  # ambil nama channel
    user    = query.from_user
    member  = await ensure_member(user.id, user.username, user.full_name)
    if not member: return

    member_id = member["id"]
    name      = member.get("full_name", user.first_name or "Teman")
    ch_label  = CHANNELS.get(channel, {}).get("label", channel)

    await query.edit_message_reply_markup(reply_markup=None)
    await ctx.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"🦆 {name} pilih *{ch_label}*! Sebentar ya, Kwek siapkan... ✨",
        parse_mode="Markdown"
    )

    # Generate konten untuk channel ini
    content = await generate_channel_content(channel, name)

    # Simpan ke display_queue
    await save_display_queue(member_id, channel, content)

    # Kirim ke grup
    await ctx.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"*{ch_label}*\n\n{content}",
        parse_mode="Markdown"
    )
    await log_activity(member_id, "channel_selected", {"channel": channel})

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    await log_activity(member["id"] if member else "0", "start")
    await update.message.reply_text(
        "*Halo dari Kwek!*\n\n"
        "Aku asisten keluarga kalian. Ini yang bisa aku lakukan:\n\n"
        "*Keuangan:*\n"
        "`catat beli baso 15rb` · `masuk gajian 5jt`\n"
        "`-50rb bayar parkir` · `+2jt bonus`\n"
        "/struk + foto untuk catat dari struk\n\n"
        "*Foto & Cerita:*\n"
        "Kirim foto momen keluarga langsung\n"
        "/foto + foto untuk simpan pengingat\n\n"
        "*Jurnal Harian:*\n"
        "/kabar — ceritakan hari ini ke Kwek\n\n"
        "*Mood Channel:*\n"
        "/channel — pilih konten hari ini\n\n"
        "*Reminder & Tugas:*\n"
        "`ingatkan bayar listrik tgl 20`\n"
        "`todo beli deterjen`\n\n"
        "/help untuk panduan lengkap",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Panduan Lengkap Kwek*\n\n"
        "*KEUANGAN*\n"
        "`catat/cata/catet [deskripsi] [nominal]`\n"
        "`masuk/gajian/dapat [deskripsi] [nominal]`\n"
        "`-15rb beli baso` · `+5jt gajian`\n"
        "/struk lalu kirim foto struk\n"
        "/keuangan Ringkasan bulan ini\n\n"
        "*FOTO & CERITA*\n"
        "Kirim foto langsung untuk cerita\n"
        "/cerita Mulai sesi cerita manual\n"
        "/selesai Setelah semua foto terkirim\n"
        "/foto lalu kirim foto pengingat\n\n"
        "*JURNAL HARIAN*\n"
        "/kabar — ceritakan hari ini\n"
        "/jurnal — lihat jurnal terbaru\n\n"
        "*MOOD CHANNEL*\n"
        "/channel — pilih konten hari ini\n"
        "Atau bot akan tanya otomatis tiap pagi!\n\n"
        "*REMINDER & TUGAS*\n"
        "`ingatkan/remind [hal] [tgl opsional]`\n"
        "`todo/tugas [deskripsi]`\n"
        "/reminder · /tugas\n\n"
        "*LAINNYA*\n"
        "/status · /keuangan · /batal",
        parse_mode="Markdown"
    )

async def cmd_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(CHANNEL_KEYBOARD)
    await update.message.reply_text(
        "🦆 *Kwek Channel — Hari Ini Mau Apa?*\n\n"
        "Pilih konten yang kamu mau, Kwek langsung siapkan!",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def cmd_kabar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess    = get_sess(chat_id)
    sess["awaiting_journal"] = True
    await update.message.reply_text(
        "🦆 *Cerita dong, hari ini gimana?*\n\n"
        "Apapun boleh — hal seru, lucu, menyebalkan, atau biasa aja.\n"
        "Kwek akan simpan jadi kenangan keluarga yang indah 📝\n\n"
        "_Balas pesan ini dengan ceritamu..._",
        parse_mode="Markdown"
    )

async def cmd_jurnal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("Kwak! Tidak bisa ambil data."); return

    journals = await db.select("v_recent_journals",
        f"member_id=eq.{member['id']}&limit=5")

    if not journals:
        await update.message.reply_text(
            "Belum ada jurnal nih.\n\nMulai dengan /kabar dan ceritakan harimu! 📝"
        ); return

    lines = ["*Jurnal Terbaru:*\n"]
    for j in journals:
        tgl  = j.get("journal_date", "")
        text = j.get("curated_text") or j.get("raw_text", "")
        mood_map = {5:"🤩",4:"😊",3:"😌",2:"😔",1:"😢"}
        mood = mood_map.get(j.get("mood_score", 3), "😌")
        lines.append(f"{mood} *{tgl}*\n_{text[:120]}{'...' if len(text)>120 else ''}_\n")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_struk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_sess(update.effective_chat.id)["mode"] = "awaiting_struk"
    await update.message.reply_text(
        "*Mode Struk Aktif*\n\nKirim foto struk/nota sekarang ya!",
        parse_mode="Markdown"
    )

async def cmd_foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_sess(update.effective_chat.id)["mode"] = "awaiting_misc"
    await update.message.reply_text(
        "*Mode Foto Pengingat*\n\nKirim foto yang mau disimpan.",
        parse_mode="Markdown"
    )

async def cmd_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("Kwak! Tidak bisa ambil data."); return
    items = await db.select("reminders",
        f"target_member_id=eq.{member['id']}&is_active=eq.true&order=created_at.desc&limit=10")
    if not items:
        await update.message.reply_text(
            "Belum ada pengingat aktif.\n\nTambah: `ingatkan bayar listrik tgl 20`",
            parse_mode="Markdown"); return
    lines = ["*Pengingat Aktif:*\n"]
    for i, r in enumerate(items, 1):
        due = f" ({r['trigger_time'][:10]})" if r.get("trigger_time") else ""
        lines.append(f"{i}. {r['title']}{due}")
        if r.get("message"): lines.append(f"   _{r['message']}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_tugas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("Kwak! Tidak bisa ambil data."); return
    items = await db.select("tasks",
        f"assigned_to=eq.{member['id']}&status=eq.pending&order=created_at.desc&limit=10")
    if not items:
        await update.message.reply_text(
            "Tidak ada tugas pending.\n\nTambah: `todo beli deterjen`",
            parse_mode="Markdown"); return
    icons = {"high":"🔴 Urgent","medium":"🟡 Normal","low":"🟢 Santai"}
    lines = ["*Tugas Pending:*\n"]
    for i, t in enumerate(items, 1):
        p = icons.get(t.get("priority","medium"),"🟡 Normal")
        lines.append(f"{i}. [{p}] {t['title']}")
        if t.get("description"): lines.append(f"   _{t['description']}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cerita(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Dua Cara Buat Cerita:*\n\n"
        "📸 */ceritalengkap* — Kirim foto, Kwek wawancara, cerita jadi lebih kaya\n"
        "⚡ */ceritacepat* — Kirim foto + tulismu sendiri, Kwek langsung simpan\n\n"
        "_Pilih salah satu ya!_",
        parse_mode="Markdown"
    )

async def cmd_ceritalengkap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reset_sess(chat_id)
    sess = get_sess(chat_id)
    sess["mode"] = "collecting"
    sess["story_mode"] = "interview"
    await update.message.reply_text(
        "*Sesi Cerita Lengkap Dimulai!*\n\n"
        "Kirim foto-foto momen kalian (2-8 foto).\n"
        "Setelah semua foto, ketik /selesai ya!\n\n"
        "_Kwek akan wawancara untuk menggali cerita lebih dalam_ 🎬",
        parse_mode="Markdown"
    )

async def cmd_ceritacepat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reset_sess(chat_id)
    sess = get_sess(chat_id)
    sess["mode"] = "collecting"
    sess["story_mode"] = "quick"
    await update.message.reply_text(
        "*Mode Cerita Cepat!*\n\n"
        "1️⃣ Kirim foto momennya (1-8 foto)\n"
        "2️⃣ Ketik /selesai\n"
        "3️⃣ Tulis ceritamu — Kwek langsung simpan!\n\n"
        "_Tidak ada wawancara, langsung jadi_ ⚡",
        parse_mode="Markdown"
    )

async def cmd_selesai(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess    = get_sess(chat_id)
    if sess["mode"] != "collecting":
        await update.message.reply_text("Belum ada sesi foto aktif. Ketik /ceritalengkap atau /ceritacepat dulu!"); return
    if len(sess["photos"]) < 1:
        await update.message.reply_text("Minimal 1 foto dulu ya!"); return
    n = len(sess["photos"])
    # Mode cerita cepat — minta teks cerita dari user
    if sess.get("story_mode") == "quick":
        sess["mode"] = "awaiting_story_text"
        await update.message.reply_text(
            f"✅ *{n} foto diterima!*\n\nSekarang tulis ceritamu — bebas, natural, dari hati.\nKwek akan simpan jadi kenangan indah keluarga 📝",
            parse_mode="Markdown"
        )
        return
    # Mode interview lengkap
    sess["mode"] = "interviewing"
    await update.message.reply_text(f"{n} foto diterima! Sebentar, aku analisis...")
    try:
        q = await call_claude(
            messages=[{"role": "user", "content": f"Ini {n} foto momen keluarga. Analisis dan mulai interview dengan 1 pertanyaan pertama yang paling personal dan spesifik."}],
            system=SYSTEM_KWEK, images=sess["photos"]
        )
        sess["history"].append({"role": "assistant", "content": q})
        sess["q_count"] = 1
        await update.message.reply_text(q)
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("Kwak! Ada gangguan. Coba /ceritalengkap lagi ya.")
        reset_sess(chat_id)

async def cmd_batal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess    = get_sess(chat_id)
    # Kalau sedang interview, tawari simpan langsung
    if sess["mode"] == "interviewing" and sess["photos"]:
        reset_sess(chat_id)
        await update.message.reply_text(
            "Sesi dibatalkan.\n\n"
            "💡 _Untuk cerita cepat tanpa interview: kirim foto + tulis caption langsung!_",
            parse_mode="Markdown"
        )
    else:
        reset_sess(chat_id)
        await update.message.reply_text(
            "Sesi dibatalkan. Ketik /cerita untuk mulai lagi!\n\n"
            "💡 _Tips: Kirim foto + caption untuk simpan cerita instan!_",
            parse_mode="Markdown"
        )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("Kwak! Tidak bisa ambil data."); return
    mid      = member["id"]
    stories  = await db.select("stories",              f"author_id=eq.{mid}&select=id")
    txns     = await db.select("finance_transactions", f"recorded_by=eq.{mid}&select=id")
    photos   = await db.select("story_photos",         "select=id")
    tasks    = await db.select("tasks",                f"assigned_to=eq.{mid}&status=eq.pending&select=id")
    rems     = await db.select("reminders",            f"target_member_id=eq.{mid}&is_active=eq.true&select=id")
    journals = await db.select("daily_journal",        f"author_id=eq.{mid}&select=id")
    await update.message.reply_text(
        f"*Status Kwek Family System*\n\n"
        f"Nama: {member.get('full_name','-')}\n"
        f"Cerita: {len(stories)}\n"
        f"Foto tersimpan: {len(photos)}\n"
        f"Transaksi: {len(txns)}\n"
        f"Jurnal harian: {len(journals)}\n"
        f"Tugas pending: {len(tasks)}\n"
        f"Pengingat aktif: {len(rems)}\n\n"
        f"Database: Supabase aktif\nAI: Claude aktif",
        parse_mode="Markdown"
    )

async def cmd_keuangan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("Kwak! Tidak bisa ambil data."); return
    month_start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    txns = await db.select("finance_transactions",
        f"recorded_by=eq.{member['id']}&created_at=gte.{month_start}&select=type,amount,description")
    income  = sum(t["amount"] for t in txns if t["type"] == "income")
    expense = sum(t["amount"] for t in txns if t["type"] == "expense")
    balance = income - expense
    def fmt(n): return f"Rp {n:,.0f}".replace(",",".")
    await update.message.reply_text(
        f"*Keuangan Bulan Ini*\n\n"
        f"Pemasukan: {fmt(income)}\n"
        f"Pengeluaran: {fmt(expense)}\n"
        f"{'Surplus' if balance>=0 else 'Defisit'}: {fmt(abs(balance))}\n\n"
        f"Total {len(txns)} transaksi.",
        parse_mode="Markdown"
    )

# ─── PHOTO HANDLER ───────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user    = update.effective_user
    sess    = get_sess(chat_id)

    photo_file = await ctx.bot.get_file(update.message.photo[-1].file_id)
    async with httpx.AsyncClient() as c:
        r         = await c.get(photo_file.file_path)
        raw_bytes = r.content
        b64       = base64.b64encode(raw_bytes).decode()

    member    = await ensure_member(user.id, user.username, user.full_name)
    member_id = member["id"] if member else "unknown"

    if sess["mode"] == "awaiting_struk":
        reset_sess(chat_id)
        await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
        await update.message.reply_text("Membaca struk...")
        photo_url = await upload_photo(raw_bytes, "finance", member_id)
        parsed    = await call_claude_json(
            messages=[{"role": "user", "content": "Baca struk ini. Extract total bayar, nama toko, deskripsi belanja."}],
            system=SYSTEM_FINANCE_PHOTO, images=[b64]
        )
        if parsed and parsed.get("is_transaction"):
            await save_finance(member_id, parsed, parsed.get("type","expense"), photo_url)
            await reply_finance(update, parsed, parsed.get("type","expense"), "\nFoto struk tersimpan")
        else:
            await update.message.reply_text(
                "Foto tersimpan, tapi tidak bisa baca total.\nCatat manual: `catat belanja 50rb`",
                parse_mode="Markdown")
        await log_activity(member_id, "struk_photo", {})
        return

    if sess["mode"] == "awaiting_misc":
        reset_sess(chat_id)
        photo_url = await upload_photo(raw_bytes, "misc", member_id)
        await db.insert("bot_activity_log", {
            "action_type": "photo_misc", "action_detail": {"photo_url": photo_url}
        })
        await update.message.reply_text("Foto tersimpan sebagai pengingat!")
        return

    if sess["mode"] == "collecting":
        if len(sess["photos"]) >= 8:
            await update.message.reply_text("Sudah 8 foto! Ketik /selesai untuk lanjut."); return
        sess["photos"].append(b64)
        sess["photo_bytes"].append(raw_bytes)
        n   = len(sess["photos"])
        msg = "Foto 1 masuk! Kirim lagi ya (min. 2 foto)." if n == 1 else f"Foto {n} masuk. Kirim lagi atau /selesai."
        await update.message.reply_text(msg)
        return

    # Foto dengan caption → langsung simpan sebagai cerita tanpa interview
    caption = update.message.caption
    if caption and len(caption.strip()) > 5:
        await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
        await update.message.reply_text("📸 Ada caption! Langsung aku simpan sebagai cerita...")
        photo_url = await upload_photo(raw_bytes, "stories", member_id)
        photo_urls = [photo_url] if photo_url else []
        story_text = caption.strip()
        saved = await save_story_db(member_id, story_text, photo_urls)
        story_id = saved["id"] if saved else "?"
        await update.message.reply_text(
            f"✅ *Cerita tersimpan!*\n\n_{story_text}_\n\nID: #{story_id}",
            parse_mode="Markdown"
        )
        await log_activity(member_id, "story_quick", {"story_id": story_id})
        return

    # Foto tanpa command — tanya mau diapakan
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Cerita Cepat", callback_data="foto_ceritacepat"),
         InlineKeyboardButton("🎬 Cerita + Wawancara", callback_data="foto_ceritalengkap")],
        [InlineKeyboardButton("📌 Reminder / Pengingat", callback_data="foto_reminder"),
         InlineKeyboardButton("📋 Log Aktivitas", callback_data="foto_log")],
    ])
    sess["pending_b64"]   = b64
    sess["pending_bytes"] = raw_bytes
    await update.message.reply_text("📸 Foto masuk! Mau diapakan?", reply_markup=keyboard)
    await update.message.reply_text(
        "Foto masuk! Kirim semua foto momennya dulu.\nSudah semua? Ketik /selesai!\n\n"
        "_💡 Tips: Kirim foto + caption langsung untuk simpan cerita tanpa interview!_",
        parse_mode="Markdown"
    )

# ─── TEXT HANDLER ────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user    = update.effective_user
    sess    = get_sess(chat_id)
    text    = update.message.text.strip()

    member    = await ensure_member(user.id, user.username, user.full_name)
    member_id = member["id"] if member else "unknown"
    name      = member.get("full_name", user.first_name or "Teman") if member else "Teman"

    # ── Mode interview cerita ──
    if sess["mode"] == "interviewing":
        sess["history"].append({"role": "user", "content": text})
        MAX_Q = 4
        if sess["q_count"] >= MAX_Q:
            await update.message.reply_text("Oke! Sekarang aku buatkan ceritanya...")
            await generate_story(update, ctx, sess, member_id)
            reset_sess(chat_id)
            return
        try:
            await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
            is_last    = sess["q_count"] == MAX_Q - 1
            system_add = f"\nPertanyaan ke-{sess['q_count']+1} dari {MAX_Q}."
            system_add += " Pertanyaan TERAKHIR, tutup dengan hangat." if is_last else " Lanjut pertanyaan berikutnya."
            reply = await call_claude(messages=sess["history"], system=SYSTEM_KWEK + system_add)
            sess["history"].append({"role": "assistant", "content": reply})
            sess["q_count"] += 1
            await update.message.reply_text(reply)
        except Exception as e:
            logger.error(e)
            await update.message.reply_text("Koneksi bermasalah, coba lagi ya!")
        return

    # ── Mode awaiting story text (cerita cepat) ──
    if sess.get("mode") == "awaiting_story_text":
        await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
        await update.message.reply_text("✍️ Kwek simpan ceritamu...")
        photo_urls = []
        for raw_bytes in sess["photo_bytes"]:
            url = await upload_photo(raw_bytes, "stories", member_id)
            if url: photo_urls.append(url)
        saved    = await save_story_db(member_id, text, photo_urls)
        story_id = saved["id"] if saved else "?"
        reset_sess(chat_id)
        await update.message.reply_text(
            f"\u2705 *Cerita tersimpan!*\n\n_{text[:200]}_\n\n\U0001f4f8 {len(photo_urls)} foto \u00b7 ID: #{story_id}",
            parse_mode="Markdown"
        )
        await log_activity(member_id, "story_quick", {"story_id": story_id})
        return

    # ── Mode awaiting journal ──
    if sess.get("awaiting_journal"):
        sess["awaiting_journal"] = False
        await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
        await update.message.reply_text("🦆 Makasih udah cerita! Kwek tulis jadi kenangan indah...")

        curated = await curate_journal(text, name)
        await save_journal(member_id, text, curated, 3, "kabar")

        await update.message.reply_text(
            f"📝 *Jurnal {name} — Hari Ini*\n\n_{curated}_\n\n"
            f"Tersimpan permanen di memori keluarga Kwek 💛",
            parse_mode="Markdown"
        )
        await log_activity(member_id, "journal_add", {"length": len(text)})
        return

    # ── Deteksi trigger ──
    trigger = detect_trigger(text)
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")

    if trigger in ("expense", "income"):
        amount, desc = parse_amount_from_text(text)
        if amount > 0:
            parsed = await call_claude_json(
                messages=[{"role": "user", "content": text}],
                system=SYSTEM_FINANCE_TEXT
            ) or {"amount": amount, "description": desc, "category": "Lainnya", "merchant": None}
            parsed["amount"] = abs(parsed.get("amount", amount))
            await save_finance(member_id, parsed, trigger)
            await reply_finance(update, parsed, trigger)
            await log_activity(member_id, f"finance_{trigger}", {"amount": parsed["amount"]})
        else:
            tip = "`catat beli baso 15rb`" if trigger == "expense" else "`masuk gajian 5jt`"
            await update.message.reply_text(f"Hmm, tidak bisa baca nominalnya.\nContoh: {tip}", parse_mode="Markdown")
        return

    if trigger == "reminder":
        parsed = await call_claude_json(messages=[{"role": "user", "content": text}], system=SYSTEM_REMINDER)
        if parsed and parsed.get("title"):
            await save_reminder_db(member_id, parsed)
            due = f" ({parsed['due_date']})" if parsed.get("due_date") else ""
            await update.message.reply_text(
                f"*Pengingat Disimpan!*\n\n{parsed['title']}{due}\n/reminder untuk lihat semua.",
                parse_mode="Markdown")
            await log_activity(member_id, "reminder_add", {"title": parsed.get("title")})
        else:
            await update.message.reply_text("Gagal simpan pengingat. Coba lagi ya!")
        return

    if trigger == "task":
        parsed = await call_claude_json(messages=[{"role": "user", "content": text}], system=SYSTEM_TASK)
        if parsed and parsed.get("title"):
            await save_task_db(member_id, parsed)
            p_label = {"high":"Urgent","medium":"Normal","low":"Santai"}.get(parsed.get("priority","medium"),"Normal")
            await update.message.reply_text(
                f"*Tugas Ditambahkan!*\n\n{parsed['title']}\nPrioritas: {p_label}\n/tugas untuk lihat semua.",
                parse_mode="Markdown")
            await log_activity(member_id, "task_add", {"title": parsed.get("title")})
        else:
            await update.message.reply_text("Gagal simpan tugas. Coba lagi ya!")
        return

    # ── Chat biasa ──
    try:
        sess["general"].append({"role": "user", "content": text})
        if len(sess["general"]) > 12:
            sess["general"] = sess["general"][-12:]
        reply = await call_claude(messages=sess["general"], system=SYSTEM_KWEK)
        sess["general"].append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply)
        await log_activity(member_id, "general_chat", {"text": text[:100]})
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("Kwak! Ada gangguan sebentar. Coba lagi ya!")

# ─── STORY GENERATOR ─────────────────────────────────────────────────────────
async def generate_story(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sess: dict, member_id: str):
    await update.message.reply_text("Mengupload foto ke storage...")
    photo_urls = []
    logger.info(f"generate_story: mulai upload {len(sess['photo_bytes'])} foto untuk member {member_id}")
    for i, raw_bytes in enumerate(sess["photo_bytes"]):
        url = await upload_photo(raw_bytes, "stories", member_id)
        if url:
            photo_urls.append(url)
            logger.info(f"generate_story: foto {i+1} upload OK → {url}")
        else:
            logger.error(f"generate_story: foto {i+1} GAGAL upload")

    logger.info(f"generate_story: {len(photo_urls)}/{len(sess['photos'])} foto terupload")
    await update.message.reply_text(f"{len(photo_urls)}/{len(sess['photos'])} foto tersimpan!\nMenulis cerita...")

    qa = "\n".join([
        f"{'Keluarga' if m['role']=='user' else 'Kwek'}: {m['content']}"
        for m in sess["history"] if isinstance(m.get("content"), str)
    ])
    n      = len(sess["photos"])
    prompt = (
        f"Berdasarkan {n} foto dan hasil interview:\n\n{qa}\n\n"
        "Buatkan cerita keluarga yang indah.\n\n"
        "[JUDUL CERITA PUITIS]\n\n[Paragraf pembuka 3 kalimat]\n\n"
        + "\n\n".join([f"Momen {i+1}\n[Narasi foto {i+1}]" for i in range(n)])
        + "\n\n[Kalimat penutup bermakna]\n\n#tag1 #tag2 #tag3 #tag4"
    )
    try:
        story    = await call_claude(messages=[{"role": "user", "content": prompt}], system=SYSTEM_STORY, images=sess["photos"][:4])
        saved    = await save_story_db(member_id, story, photo_urls)
        story_id = saved["id"] if saved else "?"
        await update.message.reply_text(f"*CERITA KALIAN SUDAH JADI!*\n\n{story}", parse_mode="Markdown")
        await update.message.reply_text(f"Cerita tersimpan (ID: #{story_id})\n{len(photo_urls)} foto aman di storage.\n\nKetik /cerita untuk buat cerita baru!")
        await log_activity(member_id, "story_created", {"story_id": story_id})
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("Aduh gagal buat cerita! Coba /cerita lagi ya.")

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    for cmd, fn in [
        ("start", cmd_start), ("help", cmd_help), ("cerita", cmd_cerita),
        ("selesai", cmd_selesai), ("batal", cmd_batal), ("status", cmd_status),
        ("ceritalengkap", cmd_ceritalengkap), ("ceritacepat", cmd_ceritacepat),
        ("keuangan", cmd_keuangan), ("struk", cmd_struk), ("foto", cmd_foto),
        ("reminder", cmd_reminder), ("tugas", cmd_tugas),
        ("channel", cmd_channel), ("kabar", cmd_kabar), ("jurnal", cmd_jurnal),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    # Handlers
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Scheduler — Bot Wartawan (WIB = UTC+7)
    job_queue = app.job_queue
    job_queue.run_daily(job_pagi,  time=time(23, 30, 0, tzinfo=timezone.utc))  # 06.30 WIB
    job_queue.run_daily(job_sore,  time=time(10,  0, 0, tzinfo=timezone.utc))  # 17.00 WIB
    job_queue.run_daily(job_malam, time=time(13, 30, 0, tzinfo=timezone.utc))  # 20.30 WIB

    logger.info("Kwek Bot running! wartawan + mood channel + journal + scheduler")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
