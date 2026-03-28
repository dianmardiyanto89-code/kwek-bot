import os
import logging
import base64
import httpx
import json
import re
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── ENV VARIABLES ───────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "").strip()
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY   = os.environ.get("SUPABASE_KEY", "").strip()
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

if not TELEGRAM_TOKEN: raise ValueError("TELEGRAM_TOKEN tidak ditemukan!")
if not CLAUDE_API_KEY:  raise ValueError("CLAUDE_API_KEY tidak ditemukan!")
if not SUPABASE_URL:    raise ValueError("SUPABASE_URL tidak ditemukan!")
if not SUPABASE_KEY:    raise ValueError("SUPABASE_KEY tidak ditemukan!")

# ─── TRIGGER DETECTION ────────────────────────────────────────────────────────
FINANCE_EXPENSE_WORDS = {"catat", "cata", "catet", "keluar", "keluarin", "bayar", "beli"}
FINANCE_INCOME_WORDS  = {"masuk", "dapat", "terima", "gaji", "gajian", "pemasukan"}
REMINDER_WORDS        = {"ingatkan", "ingatin", "reminder", "remind", "inget", "pengingat"}
TASK_WORDS            = {"todo", "tugas", "kerjain", "task"}

FINANCE_SYMBOL_RE = re.compile(r'^([+\-])(\d[\d.,]*[kKmMrRbB]*)\s+(.+)$')

def detect_trigger(text: str) -> str:
    t          = text.strip().lower()
    first_word = t.split()[0] if t.split() else ""

    if FINANCE_SYMBOL_RE.match(text.strip()):
        return "income" if text.strip()[0] == "+" else "expense"
    if first_word in FINANCE_INCOME_WORDS:
        return "income"
    if first_word in FINANCE_EXPENSE_WORDS:
        return "expense"
    for word in FINANCE_EXPENSE_WORDS | FINANCE_INCOME_WORDS:
        if _edit_distance(first_word, word) == 1 and len(first_word) >= 4:
            return "income" if word in FINANCE_INCOME_WORDS else "expense"
    if first_word in REMINDER_WORDS or any(w in t for w in REMINDER_WORDS):
        return "reminder"
    if first_word in TASK_WORDS:
        return "task"
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
    if m:
        return _parse_number(m.group(2)), m.group(3).strip()
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
    if re.search(r'(rb|ribu|k)$', s):
        mult = 1000;       s = re.sub(r'(rb|ribu|k)$', '', s)
    elif re.search(r'(jt|juta|j)$', s):
        mult = 1_000_000;  s = re.sub(r'(jt|juta|j)$', '', s)
    elif re.search(r'(m)$', s):
        mult = 1_000_000;  s = re.sub(r'm$', '', s)
    try:
        return abs(int(float(s) * mult))
    except Exception:
        return 0

# ─── SUPABASE CLIENT ──────────────────────────────────────────────────────────
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

# ─── SESSION ──────────────────────────────────────────────────────────────────
sessions = {}

def get_sess(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = {
            "mode": "idle", "photos": [], "photo_bytes": [],
            "photo_urls": [], "history": [], "q_count": 0, "general": []
        }
    return sessions[chat_id]

def reset_sess(chat_id):
    sessions[chat_id] = {
        "mode": "idle", "photos": [], "photo_bytes": [],
        "photo_urls": [], "history": [], "q_count": 0, "general": []
    }

# ─── CLAUDE ───────────────────────────────────────────────────────────────────
SYSTEM_KWEK = """Kamu adalah Kwek, asisten keluarga yang ceria, hangat, cerdas, dan sedikit lucu.
Berbicara Bahasa Indonesia yang natural dan santai. Gunakan emoji secukupnya.

Kemampuan:
1. SUTRADARA CERITA: Interview keluarga berdasarkan foto, lalu buatkan cerita indah
2. ASISTEN UMUM: Jawab pertanyaan apapun dengan ramah
3. Info fitur: Keuangan (catat/masuk), Reminder (ingatkan), Tugas (todo)

Saat interview foto:
- Analisis foto dengan empati dan detail
- Pertanyaan personal, spesifik, hangat - bukan generik
- Fokus pada emosi dan momen unik
- Maksimal 4 pertanyaan sebelum buat cerita"""

SYSTEM_STORY = """Kamu adalah penulis cerita keluarga yang puitis dan berbakat.
Tulis cerita berdasarkan foto dan hasil interview.
Gunakan Bahasa Indonesia yang indah, personal, menyentuh tapi tidak berlebihan.
Cerita harus terasa seperti ditulis oleh seseorang yang benar-benar hadir di momen itu."""

SYSTEM_FINANCE_PHOTO = """Kamu adalah parser keuangan untuk foto struk/nota belanja.
Balas HANYA dengan JSON valid, tanpa teks lain.

{
  "is_transaction": true,
  "amount": 321200,
  "type": "expense",
  "description": "belanja Indomaret",
  "category": "Belanja",
  "merchant": "Indomaret",
  "notes": null
}

ATURAN: amount = TOTAL BAYAR FINAL setelah diskon, selalu positif.
Jika tidak bisa baca struk: is_transaction false.
Kategori: Makanan & Minuman, Transportasi, Belanja, Kesehatan, Pendidikan, Hiburan, Tagihan & Utilitas, Tabungan & Investasi, Pemasukan, Lainnya"""

SYSTEM_FINANCE_TEXT = """Kamu adalah parser keuangan untuk teks chat.
Balas HANYA dengan JSON valid, tanpa teks lain.

{
  "amount": 15000,
  "description": "beli baso",
  "category": "Makanan & Minuman",
  "merchant": null,
  "notes": null
}

ATURAN: amount selalu positif. 15rb=15000, 5jt=5000000.
Kategori: Makanan & Minuman, Transportasi, Belanja, Kesehatan, Pendidikan, Hiburan, Tagihan & Utilitas, Tabungan & Investasi, Pemasukan, Lainnya"""

SYSTEM_REMINDER = """Kamu adalah parser pengingat keluarga.
Balas HANYA dengan JSON valid, tanpa teks lain.

{
  "title": "Bayar listrik",
  "notes": "Jatuh tempo tanggal 20",
  "due_date": "2026-04-20"
}

due_date format YYYY-MM-DD jika disebutkan, null jika tidak ada.
Hilangkan kata trigger (ingatkan, remind, dll) dari title."""

SYSTEM_TASK = """Kamu adalah parser tugas keluarga.
Balas HANYA dengan JSON valid, tanpa teks lain.

{
  "title": "Beli deterjen",
  "notes": "Merk Rinso ukuran besar",
  "priority": "medium"
}

priority: high=urgent/penting, medium=default, low=santai.
Hilangkan kata trigger (todo, tugas, dll) dari title."""

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
        return json.loads(clean)
    except Exception as e:
        logger.error(f"call_claude_json: {e}")
        return None

# ─── DB HELPERS ───────────────────────────────────────────────────────────────
async def ensure_member(telegram_id: int, username: str = None, full_name: str = None) -> dict | None:
    existing = await db.select("family_members", f"telegram_id=eq.{telegram_id}")
    if existing:
        return existing[0]
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
        "recorded_by": member_id,
        "type":        tx_type,
        "amount":      abs(parsed.get("amount", 0)),
        "description": parsed.get("description", ""),
        "category_id": cat_id,
        "merchant":    parsed.get("merchant"),
        "notes":       parsed.get("notes"),
        "receipt_url": photo_url,
        "source":      "photo" if photo_url else "bot"
    })

async def save_story_db(member_id: str, story_text: str, photo_urls: list) -> dict | None:
    title = "Cerita Keluarga"
    for line in story_text.split("\n"):
        line = line.strip().replace("*", "").replace("_", "")
        if line and 5 < len(line) < 80 and not line.startswith("#"):
            title = line; break
    story = await db.insert("stories", {
        "member_id":   member_id, "title": title, "content": story_text,
        "photo_count": len(photo_urls), "mood": "happy", "tags": ["keluarga", "momen"]
    })
    if story and photo_urls:
        for i, url in enumerate(photo_urls):
            await db.insert("story_photos", {"story_id": story["id"], "photo_url": url, "order_index": i+1})
    return story

async def save_reminder_db(member_id: str, parsed: dict) -> dict | None:
    data = {
        "target_member_id": member_id,
        "title":            parsed.get("title", "Pengingat"),
        "message":          parsed.get("notes"),
        "is_active":        True,
        "created_by":       member_id,
        "reminder_type":    "manual"
    }
    if parsed.get("due_date"):
        data["trigger_time"] = parsed["due_date"] + "T08:00:00+07:00"
    return await db.insert("reminders", data)

async def save_task_db(member_id: str, parsed: dict) -> dict | None:
    return await db.insert("tasks", {
        "assigned_to": member_id,
        "created_by":  member_id,
        "title":       parsed.get("title", "Tugas baru"),
        "description": parsed.get("notes"),
        "priority":    parsed.get("priority", "medium"),
        "status":      "pending",
        "source":      "bot"
    })

async def log_activity(member_id: str, action: str, details: dict = None):
    await db.insert("bot_activity_log", {"member_id": member_id, "action_type": action, "details": details or {}})

# ─── REPLY HELPERS ────────────────────────────────────────────────────────────
async def reply_finance(update: Update, parsed: dict, tx_type: str, extra: str = ""):
    amt     = abs(parsed.get("amount", 0))
    desc    = parsed.get("description", "")
    cat     = parsed.get("category", "Lainnya")
    merch   = parsed.get("merchant")
    emoji   = "Pemasukan" if tx_type == "income" else "Pengeluaran"
    m_line  = f"\nToko: {merch}" if merch else ""
    await update.message.reply_text(
        f"*{emoji} Tercatat!*\n\n{desc}: Rp {amt:,.0f}{m_line}\nKategori: {cat}{extra}",
        parse_mode="Markdown"
    )

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    await log_activity(member["id"] if member else "0", "start")
    await update.message.reply_text(
        "*Halo dari Kwek!*\n\n"
        "Aku asisten keluarga kalian. Ini yang bisa aku lakukan:\n\n"
        "*Keuangan:*\n"
        "`catat beli baso 15rb`\n"
        "`masuk gajian 5jt`\n"
        "`-50rb bayar parkir` atau `+2jt bonus`\n"
        "/struk + foto untuk catat belanja dari struk\n\n"
        "*Foto & Cerita:*\n"
        "Kirim foto momen keluarga langsung\n"
        "/foto + foto untuk simpan pengingat\n\n"
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
        "`catat/cata/catet [deskripsi] [nominal]` Pengeluaran\n"
        "`masuk/gajian/dapat [deskripsi] [nominal]` Pemasukan\n"
        "`-15rb beli baso` Pengeluaran (simbol minus)\n"
        "`+5jt gajian` Pemasukan (simbol plus)\n"
        "/struk lalu kirim foto struk\n"
        "/keuangan Ringkasan bulan ini\n\n"
        "*FOTO & CERITA*\n"
        "Kirim foto langsung untuk cerita\n"
        "/cerita Mulai sesi cerita manual\n"
        "/selesai Setelah semua foto terkirim\n"
        "/foto lalu kirim foto pengingat\n\n"
        "*REMINDER*\n"
        "`ingatkan/remind [hal] [tgl opsional]`\n"
        "/reminder Lihat daftar pengingat\n\n"
        "*TUGAS*\n"
        "`todo/tugas [deskripsi]`\n"
        "/tugas Lihat daftar tugas pending\n\n"
        "*LAINNYA*\n"
        "/status Statistik sistem\n"
        "/batal Batalkan sesi aktif",
        parse_mode="Markdown"
    )

async def cmd_struk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_sess(update.effective_chat.id)["mode"] = "awaiting_struk"
    await update.message.reply_text(
        "*Mode Struk Aktif*\n\nKirim foto struk/nota sekarang ya!\nKwek akan baca totalnya otomatis.",
        parse_mode="Markdown"
    )

async def cmd_foto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_sess(update.effective_chat.id)["mode"] = "awaiting_misc"
    await update.message.reply_text(
        "*Mode Foto Pengingat*\n\nKirim foto yang mau disimpan.\nContoh: foto kunci cadangan, barang penting, dll.",
        parse_mode="Markdown"
    )

async def cmd_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("Kwak! Tidak bisa ambil data."); return
    items = await db.select("reminders", f"target_member_id=eq.{member['id']}&is_active=eq.true&order=created_at.desc&limit=10")
    if not items:
        await update.message.reply_text("Belum ada pengingat aktif.\n\nTambah: `ingatkan bayar listrik tgl 20`", parse_mode="Markdown"); return
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
    items = await db.select("tasks", f"assigned_to=eq.{member['id']}&status=eq.pending&order=created_at.desc&limit=10")
    if not items:
        await update.message.reply_text("Tidak ada tugas pending.\n\nTambah: `todo beli deterjen`", parse_mode="Markdown"); return
    icons = {"high": "Urgent", "medium": "Normal", "low": "Santai"}
    lines = ["*Tugas Pending:*\n"]
    for i, t in enumerate(items, 1):
        p = icons.get(t.get("priority", "medium"), "Normal")
        lines.append(f"{i}. [{p}] {t['title']}")
        if t.get("notes"): lines.append(f"   _{t['notes']}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_cerita(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reset_sess(chat_id)
    get_sess(chat_id)["mode"] = "collecting"
    await update.message.reply_text(
        "*Sesi Cerita Dimulai!*\n\nKirim foto-foto momen kalian (2-8 foto).\nSetelah semua foto, ketik /selesai ya!",
        parse_mode="Markdown"
    )

async def cmd_selesai(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess    = get_sess(chat_id)
    if sess["mode"] != "collecting":
        await update.message.reply_text("Belum ada sesi foto aktif. Ketik /cerita dulu!"); return
    if len(sess["photos"]) < 2:
        await update.message.reply_text("Minimal 2 foto dulu ya!"); return
    sess["mode"] = "interviewing"
    n = len(sess["photos"])
    await update.message.reply_text(f"{n} foto diterima! Sebentar, aku analisis...")
    try:
        q = await call_claude(
            messages=[{"role": "user", "content": f"Ini {n} foto momen keluarga. Analisis dan mulai interview dengan 1 pertanyaan pertama yang paling personal dan spesifik berdasarkan apa yang kamu lihat."}],
            system=SYSTEM_KWEK, images=sess["photos"]
        )
        sess["history"].append({"role": "assistant", "content": q})
        sess["q_count"] = 1
        await update.message.reply_text(q)
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("Kwak! Ada gangguan. Coba /cerita lagi ya.")
        reset_sess(chat_id)

async def cmd_batal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_sess(update.effective_chat.id)
    await update.message.reply_text("Sesi dibatalkan. Ketik /cerita untuk mulai lagi!")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("Kwak! Tidak bisa ambil data."); return
    mid = member["id"]
    stories = await db.select("stories",              f"member_id=eq.{mid}&select=id")
    txns    = await db.select("finance_transactions", f"recorded_by=eq.{mid}&select=id")
    photos  = await db.select("story_photos",         "select=id")
    tasks   = await db.select("tasks",                f"assigned_to=eq.{mid}&status=eq.pending&select=id")
    rems    = await db.select("reminders",            f"member_id=eq.{mid}&is_active=eq.true&select=id")
    await update.message.reply_text(
        f"*Status Kwek Family System*\n\n"
        f"Nama: {member.get('full_name','-')}\n"
        f"Cerita: {len(stories)}\n"
        f"Foto tersimpan: {len(photos)}\n"
        f"Transaksi: {len(txns)}\n"
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
    def fmt(n): return f"Rp {n:,.0f}".replace(",", ".")
    await update.message.reply_text(
        f"*Keuangan Bulan Ini*\n\n"
        f"Pemasukan: {fmt(income)}\n"
        f"Pengeluaran: {fmt(expense)}\n"
        f"{'Surplus' if balance >= 0 else 'Defisit'}: {fmt(abs(balance))}\n\n"
        f"Total {len(txns)} transaksi.",
        parse_mode="Markdown"
    )

# ─── PHOTO HANDLER ────────────────────────────────────────────────────────────
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

    # /struk → langsung baca struk (skip AI deteksi konteks)
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
            await save_finance(member_id, parsed, parsed.get("type", "expense"), photo_url)
            await reply_finance(update, parsed, parsed.get("type", "expense"), "\nFoto struk tersimpan")
        else:
            await update.message.reply_text(
                "Foto tersimpan, tapi tidak bisa baca total.\nCatat manual: `catat belanja 50rb`",
                parse_mode="Markdown"
            )
        await log_activity(member_id, "struk_photo", {})
        return

    # /foto → simpan pengingat tanpa AI (0 Claude calls)
    if sess["mode"] == "awaiting_misc":
        reset_sess(chat_id)
        photo_url = await upload_photo(raw_bytes, "misc", member_id)
        await db.insert("bot_activity_log", {
            "member_id": member_id, "action_type": "photo_misc",
            "details": {"photo_url": photo_url}
        })
        await update.message.reply_text("Foto tersimpan sebagai pengingat!")
        return

    # Sesi cerita collecting
    if sess["mode"] == "collecting":
        if len(sess["photos"]) >= 8:
            await update.message.reply_text("Sudah 8 foto! Ketik /selesai untuk lanjut."); return
        sess["photos"].append(b64)
        sess["photo_bytes"].append(raw_bytes)
        n = len(sess["photos"])
        msg = "Foto 1 masuk! Kirim lagi ya (min. 2 foto)." if n == 1 else f"Foto {n} masuk. Kirim lagi atau /selesai."
        await update.message.reply_text(msg)
        return

    # Idle → otomatis mulai sesi cerita
    sess["mode"] = "collecting"
    sess["photos"].append(b64)
    sess["photo_bytes"].append(raw_bytes)
    await update.message.reply_text(
        "Foto masuk! Kirim semua foto momennya dulu.\nSudah semua? Ketik /selesai!"
    )

# ─── TEXT HANDLER ─────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user    = update.effective_user
    sess    = get_sess(chat_id)
    text    = update.message.text.strip()

    member    = await ensure_member(user.id, user.username, user.full_name)
    member_id = member["id"] if member else "unknown"

    # Mode interview cerita — tidak diinterupsi trigger
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

    # Deteksi trigger (tanpa Claude call)
    trigger = detect_trigger(text)
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")

    if trigger in ("expense", "income"):
        amount, desc = parse_amount_from_text(text)
        if amount > 0:
            # 1 Claude call untuk parse detail + kategori
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
                parse_mode="Markdown"
            )
            await log_activity(member_id, "reminder_add", {"title": parsed.get("title")})
        else:
            await update.message.reply_text("Gagal simpan pengingat. Coba lagi ya!")
        return

    if trigger == "task":
        parsed = await call_claude_json(messages=[{"role": "user", "content": text}], system=SYSTEM_TASK)
        if parsed and parsed.get("title"):
            await save_task_db(member_id, parsed)
            p_label = {"high": "Urgent", "medium": "Normal", "low": "Santai"}.get(parsed.get("priority", "medium"), "Normal")
            await update.message.reply_text(
                f"*Tugas Ditambahkan!*\n\n{parsed['title']}\nPrioritas: {p_label}\n/tugas untuk lihat semua.",
                parse_mode="Markdown"
            )
            await log_activity(member_id, "task_add", {"title": parsed.get("title")})
        else:
            await update.message.reply_text("Gagal simpan tugas. Coba lagi ya!")
        return

    # Chat biasa — 1 Claude call
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

# ─── STORY GENERATOR ──────────────────────────────────────────────────────────
async def generate_story(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sess: dict, member_id: str):
    await update.message.reply_text("Mengupload foto ke storage...")
    photo_urls = []
    for raw_bytes in sess["photo_bytes"]:
        url = await upload_photo(raw_bytes, "stories", member_id)
        if url: photo_urls.append(url)

    await update.message.reply_text(f"{len(photo_urls)}/{len(sess['photos'])} foto tersimpan!\nMenulis cerita...")

    qa = "\n".join([
        f"{'Keluarga' if m['role']=='user' else 'Kwek'}: {m['content']}"
        for m in sess["history"] if isinstance(m.get("content"), str)
    ])
    n = len(sess["photos"])
    prompt = (
        f"Berdasarkan {n} foto dan hasil interview:\n\n{qa}\n\n"
        "Buatkan cerita keluarga yang indah.\n\n"
        "[JUDUL CERITA PUITIS]\n\n[Paragraf pembuka 3 kalimat]\n\n"
        + "\n\n".join([f"Momen {i+1}\n[Narasi foto {i+1} - personal dan spesifik]" for i in range(n)])
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

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, fn in [
        ("start", cmd_start), ("help", cmd_help), ("cerita", cmd_cerita),
        ("selesai", cmd_selesai), ("batal", cmd_batal), ("status", cmd_status),
        ("keuangan", cmd_keuangan), ("struk", cmd_struk), ("foto", cmd_foto),
        ("reminder", cmd_reminder), ("tugas", cmd_tugas)
    ]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Kwek Bot running! triggers + storage + reminder + task")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
