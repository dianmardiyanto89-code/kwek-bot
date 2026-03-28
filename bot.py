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

# ─── SUPABASE CLIENT ──────────────────────────────────────────────────────────
class SupabaseClient:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }
        self.storage_headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }

    async def insert(self, table: str, data: dict) -> dict | None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.url}/rest/v1/{table}",
                headers=self.headers, json=data
            )
            if r.status_code in (200, 201):
                result = r.json()
                return result[0] if isinstance(result, list) and result else result
            logger.error(f"Supabase insert error {r.status_code}: {r.text}")
            return None

    async def select(self, table: str, query: str = "") -> list:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.url}/rest/v1/{table}?{query}",
                headers=self.headers
            )
            if r.status_code == 200:
                return r.json()
            logger.error(f"Supabase select error {r.status_code}: {r.text}")
            return []

    async def update(self, table: str, match: str, data: dict) -> bool:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.patch(
                f"{self.url}/rest/v1/{table}?{match}",
                headers=self.headers, json=data
            )
            return r.status_code in (200, 204)

    async def upload_photo(self, bucket: str, path: str, image_bytes: bytes, content_type: str = "image/jpeg") -> str | None:
        """Upload foto ke Supabase Storage, return public URL."""
        upload_headers = {
            **self.storage_headers,
            "Content-Type": content_type,
            "x-upsert": "true"
        }
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{self.url}/storage/v1/object/{bucket}/{path}",
                headers=upload_headers,
                content=image_bytes
            )
            if r.status_code in (200, 201):
                # Return URL yang bisa diakses dengan service_role key
                return f"{self.url}/storage/v1/object/{bucket}/{path}"
            logger.error(f"Storage upload error {r.status_code}: {r.text}")
            return None

    async def get_storage_stats(self, bucket: str) -> dict:
        """Ambil info ukuran storage bucket."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self.url}/storage/v1/bucket/{bucket}",
                headers=self.storage_headers
            )
            if r.status_code == 200:
                return r.json()
            return {}

db = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)

# ─── STORAGE MONITORING ───────────────────────────────────────────────────────
STORAGE_LIMIT_GB = 1.0  # Supabase free tier
STORAGE_ALERTS = [0.70, 0.85, 0.95]

async def check_storage_alert(current_bytes: int) -> str | None:
    """Return pesan alert jika mendekati batas, None jika aman."""
    ratio = current_bytes / (STORAGE_LIMIT_GB * 1024 ** 3)
    for threshold in sorted(STORAGE_ALERTS, reverse=True):
        if ratio >= threshold:
            pct = int(threshold * 100)
            used_mb = current_bytes / (1024 ** 2)
            return (
                f"⚠️ *Storage Alert {pct}%*\n"
                f"Sudah terpakai: {used_mb:.1f} MB dari {int(STORAGE_LIMIT_GB * 1024)} MB\n"
                f"Pertimbangkan untuk migrasi ke Cloudflare R2."
            )
    return None

# ─── SESSION MANAGEMENT ───────────────────────────────────────────────────────
sessions = {}

def get_sess(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = {
            "mode": "idle",
            "photos": [],        # base64 untuk Claude Vision
            "photo_bytes": [],   # raw bytes untuk upload Storage
            "photo_urls": [],    # URL setelah upload
            "history": [],
            "q_count": 0,
            "general": [],
            "context": "story"   # story | finance | misc
        }
    return sessions[chat_id]

def reset_sess(chat_id):
    sessions[chat_id] = {
        "mode": "idle",
        "photos": [], "photo_bytes": [], "photo_urls": [],
        "history": [], "q_count": 0, "general": [],
        "context": "story"
    }

# ─── CLAUDE API ───────────────────────────────────────────────────────────────
SYSTEM_KWEK = """Kamu adalah Kwek 🦆, asisten keluarga yang ceria, hangat, cerdas, dan sedikit lucu.
Kamu berbicara Bahasa Indonesia yang natural, santai, seperti teman dekat keluarga.
Gunakan emoji secukupnya. Jangan terlalu formal.

Kemampuanmu:
1. SUTRADARA CERITA: Saat ada foto, kamu interview keluarga lalu buatkan story indah
2. ASISTEN UMUM: Jawab pertanyaan apapun
3. CATAT KEUANGAN: Deteksi dan catat transaksi keuangan dari chat natural
4. PENGINGAT & CATATAN: Bantu catat hal penting

Saat mode interview story:
- Analisis foto dengan detail dan empati
- Ajukan pertanyaan personal, spesifik, dan hangat
- Fokus pada emosi, detail unik, momen lucu atau mengharukan
- Maksimal 4 pertanyaan sebelum buat cerita"""

SYSTEM_STORY = """Kamu adalah penulis cerita keluarga yang puitis, hangat, dan berbakat.
Tulis cerita berdasarkan foto dan hasil interview.
Gunakan Bahasa Indonesia yang indah, personal, menyentuh — tidak berlebihan.
Cerita harus terasa seperti ditulis oleh seseorang yang benar-benar hadir di momen itu."""

SYSTEM_FINANCE_PARSER = """Kamu adalah parser keuangan. Extract informasi transaksi dari teks.
Balas HANYA dengan JSON valid, tanpa teks lain, tanpa markdown.

Format JSON:
{
  "is_transaction": true/false,
  "amount": 35000,
  "type": "expense" atau "income",
  "description": "makan siang",
  "category": "Makanan & Minuman",
  "notes": "catatan tambahan atau null"
}

Kategori yang tersedia: Makanan & Minuman, Transportasi, Belanja, Kesehatan, Pendidikan, Hiburan, Tagihan & Utilitas, Tabungan & Investasi, Pemasukan, Lainnya

Contoh input: "beli makan 35rb" → is_transaction: true, amount: 35000, type: expense
Contoh input: "gajian 5jt" → is_transaction: true, amount: 5000000, type: income
Contoh input: "cuaca hari ini" → is_transaction: false"""

SYSTEM_PHOTO_CONTEXT = """Kamu adalah Kwek 🦆. Analisis foto yang dikirim dan tentukan konteksnya.
Balas HANYA dengan JSON valid:
{
  "context": "story" atau "finance" atau "misc",
  "description": "deskripsi singkat foto dalam 1 kalimat"
}

- story: foto momen keluarga, liburan, acara, anak-anak, dll
- finance: struk belanja, nota, tagihan, receipt
- misc: foto objek, dokumen, pengingat, kunci, dll"""

async def call_claude(messages: list, system: str, images: list = None) -> str:
    headers = {
        "Content-Type": "application/json",
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01"
    }
    if images:
        content = []
        for img_b64 in images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
            })
        last_text = messages[-1]["content"] if messages else "Analisis foto ini."
        content.append({"type": "text", "text": last_text})
        api_messages = messages[:-1] + [{"role": "user", "content": content}]
    else:
        api_messages = messages

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "system": system,
        "messages": api_messages
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(CLAUDE_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

# ─── SUPABASE HELPERS ─────────────────────────────────────────────────────────
async def ensure_member(telegram_id: int, username: str = None, full_name: str = None) -> dict | None:
    existing = await db.select("family_members", f"telegram_id=eq.{telegram_id}")
    if existing:
        return existing[0]
    return await db.insert("family_members", {
        "telegram_id": telegram_id,
        "username": username or f"user_{telegram_id}",
        "full_name": full_name or "Anggota Keluarga",
        "role": "member"
    })

async def upload_photo_to_storage(image_bytes: bytes, folder: str, member_id: int = None) -> str | None:
    """Upload foto ke Supabase Storage, return URL."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    mid = member_id or "unknown"
    path = f"{folder}/{mid}/{ts}.jpg"
    url = await db.upload_photo("photos", path, image_bytes)
    return url

async def save_story(member_id: int, story_text: str, photo_urls: list, interview_data: list) -> dict | None:
    # Extract judul dari cerita
    title = "Cerita Keluarga"
    lines = story_text.split("\n")
    for line in lines:
        line = line.strip().replace("*", "")
        if line and len(line) > 5 and len(line) < 80:
            title = line
            break

    story = await db.insert("stories", {
        "member_id": member_id,
        "title": title,
        "content": story_text,
        "photo_count": len(photo_urls),
        "mood": "happy",
        "tags": ["keluarga", "momen"]
    })

    if story and photo_urls:
        story_id = story.get("id")
        for i, url in enumerate(photo_urls):
            await db.insert("story_photos", {
                "story_id": story_id,
                "photo_url": url,
                "order_index": i + 1
            })

    return story

async def save_finance(member_id: int, parsed: dict, photo_url: str = None) -> dict | None:
    cats = await db.select("finance_categories", f"name=eq.{parsed.get('category','Lainnya')}")
    cat_id = cats[0]["id"] if cats else None

    data = {
        "member_id": member_id,
        "type": parsed.get("type", "expense"),
        "amount": parsed.get("amount", 0),
        "description": parsed.get("description", ""),
        "category_id": cat_id,
        "notes": parsed.get("notes"),
        "receipt_url": photo_url
    }
    return await db.insert("finance_transactions", data)

async def save_misc_photo(member_id: int, photo_url: str, description: str) -> dict | None:
    """Simpan foto misc ke bot_activity_log sebagai referensi."""
    return await db.insert("bot_activity_log", {
        "member_id": member_id,
        "action_type": "photo_misc",
        "details": {"photo_url": photo_url, "description": description}
    })

async def log_activity(member_id: int, action: str, details: dict = None):
    await db.insert("bot_activity_log", {
        "member_id": member_id,
        "action_type": action,
        "details": details or {}
    })

async def try_parse_finance(text: str) -> dict | None:
    try:
        result = await call_claude(
            messages=[{"role": "user", "content": text}],
            system=SYSTEM_FINANCE_PARSER
        )
        clean = result.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean)
        return parsed if parsed.get("is_transaction") else None
    except Exception as e:
        logger.error(f"Finance parse error: {e}")
        return None

async def detect_photo_context(image_b64: str) -> dict:
    """Deteksi konteks foto: story / finance / misc."""
    try:
        result = await call_claude(
            messages=[{"role": "user", "content": "Tentukan konteks foto ini."}],
            system=SYSTEM_PHOTO_CONTEXT,
            images=[image_b64]
        )
        clean = result.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except Exception:
        return {"context": "story", "description": "foto keluarga"}

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    await log_activity(member["id"] if member else 0, "start")
    await update.message.reply_text(
        "🦆 *Kwek datang!*\n\n"
        "Halo keluarga! Aku Kwek, teman setia kalian.\n\n"
        "Yang bisa aku lakukan:\n"
        "📸 *Kirim foto* → Cerita, catat struk, atau simpan pengingat\n"
        "💬 *Tanya apapun* → Aku jawab seperti asisten pribadi\n"
        "💰 *Catat pengeluaran* → Tulis natural, aku yang proses\n"
        "📖 /cerita → Mulai sesi buat cerita\n"
        "📊 /status → Statistik data keluarga\n"
        "❓ /help → Panduan lengkap\n\n"
        "Kwek siap! 🦆✨",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🦆 *Panduan Kwek*\n\n"
        "*📸 Kirim Foto:*\n"
        "Kwek otomatis deteksi jenis fotonya:\n"
        "• Foto momen → Sesi cerita\n"
        "• Struk belanja → Catat keuangan\n"
        "• Foto lain → Simpan sebagai pengingat\n\n"
        "*💰 Catat Keuangan:*\n"
        "Tulis natural: _'beli makan 35rb'_, _'gajian 5jt'_\n\n"
        "*📖 Buat Cerita:*\n"
        "1. Kirim 2-8 foto momen keluarga\n"
        "2. Jawab pertanyaan Kwek\n"
        "3. Cerita indah jadi!\n\n"
        "*⌨️ Commands:*\n"
        "/cerita - Mulai sesi cerita\n"
        "/selesai - Foto sudah semua\n"
        "/batal - Batalkan sesi\n"
        "/status - Statistik data\n"
        "/keuangan - Ringkasan keuangan\n",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("🦆 Kwak! Tidak bisa ambil data.")
        return

    stories = await db.select("stories", f"member_id=eq.{member['id']}&select=id")
    transactions = await db.select("finance_transactions", f"member_id=eq.{member['id']}&select=id")
    photos = await db.select("story_photos", "select=id")

    await update.message.reply_text(
        f"📊 *Status Kwek Family System*\n\n"
        f"👤 Nama: {member.get('full_name', '-')}\n"
        f"📖 Total cerita: {len(stories)}\n"
        f"📸 Total foto tersimpan: {len(photos)}\n"
        f"💰 Total transaksi: {len(transactions)}\n\n"
        f"🗄️ Storage: Supabase aktif ✅\n"
        f"🤖 AI: Claude aktif ✅",
        parse_mode="Markdown"
    )

async def cmd_keuangan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    member = await ensure_member(user.id, user.username, user.full_name)
    if not member:
        await update.message.reply_text("🦆 Kwak! Tidak bisa ambil data.")
        return

    now = datetime.now(timezone.utc)
    month_start = now.strftime("%Y-%m-01")
    transactions = await db.select(
        "finance_transactions",
        f"member_id=eq.{member['id']}&created_at=gte.{month_start}&select=type,amount,description"
    )

    income = sum(t["amount"] for t in transactions if t["type"] == "income")
    expense = sum(t["amount"] for t in transactions if t["type"] == "expense")
    balance = income - expense

    def fmt(n): return f"Rp {n:,.0f}".replace(",", ".")

    await update.message.reply_text(
        f"💰 *Keuangan Bulan Ini*\n\n"
        f"✅ Pemasukan: {fmt(income)}\n"
        f"🛒 Pengeluaran: {fmt(expense)}\n"
        f"{'🟢' if balance >= 0 else '🔴'} Saldo: {fmt(balance)}\n\n"
        f"Total {len(transactions)} transaksi tercatat.",
        parse_mode="Markdown"
    )

async def cmd_cerita(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reset_sess(chat_id)
    sess = get_sess(chat_id)
    sess["mode"] = "collecting"
    sess["context"] = "story"
    await update.message.reply_text(
        "🎬 *Sesi Cerita Dimulai!*\n\n"
        "Kirim foto-foto momen kalian (2-8 foto).\n"
        "Setelah semua foto terkirim, ketik */selesai* ya!\n\n"
        "🦆 _Kwek siap jadi sutradara!_",
        parse_mode="Markdown"
    )

async def cmd_selesai(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_sess(chat_id)
    if sess["mode"] != "collecting":
        await update.message.reply_text("🦆 Belum ada sesi foto aktif. Ketik /cerita dulu ya!")
        return
    if len(sess["photos"]) < 2:
        await update.message.reply_text("🦆 Minimal 2 foto dulu ya sebelum lanjut!")
        return
    sess["mode"] = "interviewing"
    n = len(sess["photos"])
    await update.message.reply_text(f"🦆 {n} foto diterima! Sebentar ya, aku analisis... 🔍")
    try:
        first_q = await call_claude(
            messages=[{"role": "user", "content": f"Ini {n} foto dari momen keluarga. Analisis semua foto dan mulai interview dengan 1 pertanyaan pertama yang paling penting dan personal. Singkat, hangat, spesifik berdasarkan apa yang kamu lihat."}],
            system=SYSTEM_KWEK,
            images=sess["photos"]
        )
        sess["history"].append({"role": "assistant", "content": first_q})
        sess["q_count"] = 1
        await update.message.reply_text(first_q)
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("🦆 Kwak! Ada gangguan. Coba /cerita lagi ya.")
        reset_sess(chat_id)

async def cmd_batal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset_sess(update.effective_chat.id)
    await update.message.reply_text("🦆 Sesi dibatalkan. Kapan saja mau mulai lagi, ketik /cerita!")

# ─── PHOTO HANDLER ────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    sess = get_sess(chat_id)

    # Download foto
    photo_file = await ctx.bot.get_file(update.message.photo[-1].file_id)
    async with httpx.AsyncClient() as client:
        r = await client.get(photo_file.file_path)
        raw_bytes = r.content
        b64 = base64.b64encode(raw_bytes).decode()

    # Jika sesi cerita sedang berjalan, tambahkan ke sesi
    if sess["mode"] == "collecting":
        if len(sess["photos"]) >= 8:
            await update.message.reply_text("🦆 Sudah 8 foto, cukup! Ketik /selesai untuk lanjut.")
            return
        sess["photos"].append(b64)
        sess["photo_bytes"].append(raw_bytes)
        n = len(sess["photos"])
        if n == 1:
            await update.message.reply_text("🦆 Foto 1 masuk! Kirim lagi ya (min. 2 foto).")
        else:
            await update.message.reply_text(
                f"🦆 Foto {n} masuk ✅\nKirim lagi atau */selesai* kalau sudah semua.",
                parse_mode="Markdown"
            )
        return

    # Foto pertama di sesi idle — deteksi konteks
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    await update.message.reply_text("🦆 Sebentar, aku analisis fotonya dulu... 🔍")

    ctx_info = await detect_photo_context(b64)
    photo_context = ctx_info.get("context", "story")
    description = ctx_info.get("description", "foto")

    member = await ensure_member(user.id, user.username, user.full_name)
    member_id = member["id"] if member else None

    if photo_context == "finance":
        # Struk / nota → upload + parse → catat keuangan
        photo_url = await upload_photo_to_storage(raw_bytes, "finance", member_id)
        await update.message.reply_text(
            f"🧾 Sepertinya ini struk/nota!\n_{description}_\n\n"
            "Sebentar, aku baca totalnya... 💰",
            parse_mode="Markdown"
        )
        try:
            parsed_text = await call_claude(
                messages=[{"role": "user", "content": "Baca struk/nota ini. Extract: nama toko, total belanja, tanggal jika ada. Lalu format sebagai transaksi keuangan."}],
                system=SYSTEM_FINANCE_PARSER,
                images=[b64]
            )
            clean = parsed_text.strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean)
            if parsed.get("is_transaction"):
                saved = await save_finance(member_id, parsed, photo_url)
                amt = parsed.get("amount", 0)
                desc = parsed.get("description", "")
                await update.message.reply_text(
                    f"✅ *Tercatat!*\n\n"
                    f"💸 {desc}: Rp {amt:,.0f}\n"
                    f"📂 Kategori: {parsed.get('category', 'Lainnya')}\n"
                    f"📸 Foto struk tersimpan ✅",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    "🦆 Foto tersimpan, tapi aku tidak bisa baca totalnya.\n"
                    "Mau catat manual? Contoh: _'beli ini 50rb'_",
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Finance photo parse error: {e}")
            await update.message.reply_text("🦆 Foto tersimpan, tapi gagal baca otomatis. Catat manual ya!")

    elif photo_context == "misc":
        # Foto pengingat / objek → upload + simpan
        photo_url = await upload_photo_to_storage(raw_bytes, "misc", member_id)
        await save_misc_photo(member_id, photo_url, description)
        await update.message.reply_text(
            f"📌 *Foto tersimpan sebagai pengingat!*\n\n"
            f"_{description}_\n\n"
            f"Foto bisa kamu lihat lagi nanti dari dashboard. ✅",
            parse_mode="Markdown"
        )

    else:
        # Foto momen → mulai sesi cerita
        sess["mode"] = "collecting"
        sess["context"] = "story"
        sess["photos"].append(b64)
        sess["photo_bytes"].append(raw_bytes)
        await update.message.reply_text(
            f"🦆 Wah, foto momen keluarga nih!\n_{description}_\n\n"
            "Kirim semua foto momennya dulu ya.\n"
            "Kalau sudah semua, ketik */selesai*! 📸",
            parse_mode="Markdown"
        )

# ─── TEXT HANDLER ─────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    sess = get_sess(chat_id)
    text = update.message.text.strip()

    member = await ensure_member(user.id, user.username, user.full_name)
    member_id = member["id"] if member else None

    if sess["mode"] == "interviewing":
        sess["history"].append({"role": "user", "content": text})
        MAX_Q = 4
        if sess["q_count"] >= MAX_Q:
            await update.message.reply_text("🦆 Oke! Sekarang aku buatkan ceritanya... ✨")
            await generate_story(update, ctx, sess, member_id)
            reset_sess(chat_id)
            return
        try:
            await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
            is_last = sess["q_count"] == MAX_Q - 1
            system_add = f"\nIni pertanyaan ke-{sess['q_count']+1} dari {MAX_Q}."
            if is_last:
                system_add += " Ini pertanyaan TERAKHIR. Balas singkat lalu tutup hangat."
            else:
                system_add += " Balas singkat lalu ajukan pertanyaan berikutnya lebih dalam."
            reply = await call_claude(messages=sess["history"], system=SYSTEM_KWEK + system_add)
            sess["history"].append({"role": "assistant", "content": reply})
            sess["q_count"] += 1
            await update.message.reply_text(reply)
        except Exception as e:
            logger.error(e)
            await update.message.reply_text("🦆 Koneksi bermasalah, coba jawab lagi ya!")
        return

    # Mode idle — coba deteksi transaksi keuangan
    await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
    parsed = await try_parse_finance(text)
    if parsed:
        saved = await save_finance(member_id, parsed)
        amt = parsed.get("amount", 0)
        desc = parsed.get("description", "")
        t_type = "💰 Pemasukan" if parsed.get("type") == "income" else "💸 Pengeluaran"
        await update.message.reply_text(
            f"✅ *Tercatat!*\n\n"
            f"{t_type}: {desc}\n"
            f"Rp {amt:,.0f}\n"
            f"📂 {parsed.get('category', 'Lainnya')}",
            parse_mode="Markdown"
        )
        await log_activity(member_id, "finance_add", {"amount": amt, "description": desc})
        return

    # Asisten umum
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
        await update.message.reply_text("🦆 Kwak! Ada gangguan sebentar. Coba lagi ya!")

# ─── STORY GENERATOR ──────────────────────────────────────────────────────────
async def generate_story(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sess: dict, member_id: int):
    chat_id = update.effective_chat.id

    # Upload semua foto ke Storage
    await update.message.reply_text("📤 Mengupload foto ke storage... ⏳")
    photo_urls = []
    for i, raw_bytes in enumerate(sess["photo_bytes"]):
        url = await upload_photo_to_storage(raw_bytes, "stories", member_id)
        if url:
            photo_urls.append(url)
            logger.info(f"Foto {i+1} uploaded: {url}")

    uploaded_count = len(photo_urls)
    await update.message.reply_text(
        f"✅ {uploaded_count}/{len(sess['photos'])} foto tersimpan!\nSekarang aku tulis ceritanya... ✍️"
    )

    qa = "\n".join([
        f"{'Keluarga' if m['role']=='user' else 'Kwek'}: {m['content']}"
        for m in sess["history"] if isinstance(m.get("content"), str)
    ])
    prompt = f"""Berdasarkan {len(sess['photos'])} foto dan hasil interview:

{qa}

Buatkan cerita keluarga yang indah. Format:

🌟 *[JUDUL CERITA PUITIS]*

[Paragraf pembuka yang kuat dan emosional — 3 kalimat]

📸 *Momen 1*
[Narasi foto pertama — personal dan spesifik]

📸 *Momen 2*
[Narasi foto kedua]

[dst untuk setiap foto — {len(sess['photos'])} foto total]

✨ _[Kalimat penutup yang bermakna]_

#tag1 #tag2 #tag3 #tag4"""

    try:
        story = await call_claude(
            messages=[{"role": "user", "content": prompt}],
            system=SYSTEM_STORY,
            images=sess["photos"][:4]
        )
        # Simpan cerita ke database
        saved = await save_story(member_id, story, photo_urls, sess["history"])
        story_id = saved["id"] if saved else "?"

        await update.message.reply_text(
            f"📖 *CERITA KALIAN SUDAH JADI!*\n\n{story}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            f"🦆 Cerita tersimpan permanen di database! (ID: #{story_id})\n"
            f"📸 {uploaded_count} foto sudah aman di storage.\n\n"
            "Ketik /cerita untuk buat cerita baru, atau tanya apapun ke Kwek ya 💛"
        )
        await log_activity(member_id, "story_created", {"story_id": story_id, "photo_count": uploaded_count})

    except Exception as e:
        logger.error(e)
        await update.message.reply_text("🦆 Aduh gagal buat cerita! Coba /cerita lagi ya.")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cerita", cmd_cerita))
    app.add_handler(CommandHandler("selesai", cmd_selesai))
    app.add_handler(CommandHandler("batal", cmd_batal))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("keuangan", cmd_keuangan))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("🦆 Kwek Bot is running with Supabase Storage!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
