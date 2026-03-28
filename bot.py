import os
import logging
import base64
import json
import re
from datetime import datetime, timezone, timedelta
import httpx
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================
# ENVIRONMENT VARIABLES
# ============================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()       # https://xxx.supabase.co
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()       # service_role key
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN tidak ditemukan!")
if not CLAUDE_API_KEY:
    raise ValueError("CLAUDE_API_KEY tidak ditemukan!")
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("⚠️ SUPABASE_URL/SUPABASE_KEY belum diset — data TIDAK akan tersimpan!")

# ============================================
# SUPABASE REST API HELPER
# ============================================
class SupabaseClient:
    """Lightweight Supabase REST client — no extra dependencies needed."""

    def __init__(self, url: str, key: str):
        self.base_url = f"{url}/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation"
        }

    async def insert(self, table: str, data: dict) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.base_url}/{table}",
                    headers=self.headers,
                    json=data
                )
                resp.raise_for_status()
                rows = resp.json()
                return rows[0] if rows else None
        except Exception as e:
            logger.error(f"❌ Supabase insert {table}: {e}")
            return None

    async def select(self, table: str, query_params: str = "", limit: int = 10) -> list:
        try:
            url = f"{self.base_url}/{table}?{query_params}&limit={limit}" if query_params else f"{self.base_url}/{table}?limit={limit}"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=self.headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"❌ Supabase select {table}: {e}")
            return []

    async def update(self, table: str, match_params: str, data: dict) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.patch(
                    f"{self.base_url}/{table}?{match_params}",
                    headers=self.headers,
                    json=data
                )
                resp.raise_for_status()
                rows = resp.json()
                return rows[0] if rows else None
        except Exception as e:
            logger.error(f"❌ Supabase update {table}: {e}")
            return None


# Initialize Supabase client
db = SupabaseClient(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# ============================================
# SESSION & SYSTEM PROMPTS
# ============================================
sessions = {}

SYSTEM_KWEK = """Kamu adalah Kwek 🦆, asisten keluarga yang ceria, hangat, cerdas, dan sedikit lucu.
Kamu berbicara Bahasa Indonesia yang natural, santai, seperti teman dekat keluarga.
Gunakan emoji secukupnya. Jangan terlalu formal.

Kemampuanmu:
1. SUTRADARA CERITA: Saat ada foto, kamu interview keluarga lalu buatkan story indah
2. ASISTEN UMUM: Jawab pertanyaan apapun — pengetahuan umum, saran, ide, dll
3. PENGINGAT & CATATAN: Bantu catat hal penting yang disebutkan keluarga
4. CATAT KEUANGAN: Saat user bilang pengeluaran, catat dengan format yang rapi

Saat mode interview story:
- Analisis foto dengan detail dan empati
- Ajukan pertanyaan personal, spesifik, dan hangat — bukan generik
- Fokus pada emosi, detail unik, momen lucu atau mengharukan
- Maksimal 4 pertanyaan sebelum buat cerita"""

SYSTEM_STORY = """Kamu adalah penulis cerita keluarga yang puitis, hangat, dan berbakat.
Tulis cerita berdasarkan foto dan hasil interview.
Gunakan Bahasa Indonesia yang indah, personal, menyentuh — tidak berlebihan.
Cerita harus terasa seperti ditulis oleh seseorang yang benar-benar hadir di momen itu."""

SYSTEM_FINANCE_PARSER = """Kamu adalah parser keuangan. Dari pesan user, extract data transaksi.
Balas HANYA dalam format JSON (tanpa markdown, tanpa penjelasan):
{
  "is_finance": true/false,
  "amount": angka dalam rupiah (integer, tanpa titik/koma),
  "type": "expense" atau "income",
  "description": "deskripsi singkat",
  "category_hint": "salah satu dari: makanan, transport, belanja_rumah, sekolah, kesehatan, hiburan, tagihan, pakaian, tabungan, lainnya",
  "merchant": "nama toko/tempat jika disebutkan",
  "payment_method": "cash/transfer/gopay/ovo/credit_card/lainnya jika disebutkan"
}

Contoh input → output:
"beli makan siang 35rb di warteg" → {"is_finance":true,"amount":35000,"type":"expense","description":"makan siang","category_hint":"makanan","merchant":"warteg","payment_method":""}
"gaji masuk 5jt" → {"is_finance":true,"amount":5000000,"type":"income","description":"gaji","category_hint":"lainnya","merchant":"","payment_method":"transfer"}
"halo kwek apa kabar" → {"is_finance":false}
"tolong buatkan cerita" → {"is_finance":false}
"""

# Category name mapping (matches seed data in DB)
CATEGORY_MAP = {
    "makanan": "Makanan & Minuman",
    "transport": "Transport",
    "belanja_rumah": "Belanja Rumah",
    "sekolah": "Sekolah & Pendidikan",
    "kesehatan": "Kesehatan & Obat",
    "hiburan": "Hiburan",
    "tagihan": "Tagihan & Utilitas",
    "pakaian": "Pakaian",
    "tabungan": "Tabungan & Investasi",
    "lainnya": "Lainnya",
}


# ============================================
# CLAUDE API CALL
# ============================================
async def call_claude(messages: list, system: str, images: list = None, max_tokens: int = 1000) -> str:
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
        "max_tokens": max_tokens,
        "system": system,
        "messages": api_messages
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(CLAUDE_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


# ============================================
# MEMBER AUTO-REGISTER
# ============================================
async def ensure_member(user) -> str | None:
    """Auto-register anggota keluarga saat pertama interaksi. Return member UUID."""
    if not db:
        return None
    existing = await db.select("family_members", f"telegram_id=eq.{user.id}", limit=1)
    if existing:
        return existing[0]["id"]
    member = await db.insert("family_members", {
        "telegram_id": user.id,
        "name": user.first_name or "Unknown",
        "full_name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
    })
    if member:
        logger.info(f"👤 Member baru terdaftar: {member['name']} ({user.id})")
        return member["id"]
    return None


# ============================================
# FINANCE HELPER
# ============================================
async def get_category_id(hint: str) -> str | None:
    if not db:
        return None
    cat_name = CATEGORY_MAP.get(hint, "Lainnya")
    cats = await db.select("finance_categories", f"name=eq.{cat_name}", limit=1)
    if cats:
        return cats[0]["id"]
    cats = await db.select("finance_categories", "name=eq.Lainnya", limit=1)
    return cats[0]["id"] if cats else None


async def try_parse_finance(text: str, user, chat_id: int) -> str | None:
    """Coba parse pesan sebagai transaksi keuangan. Return reply jika berhasil."""
    try:
        result_raw = await call_claude(
            messages=[{"role": "user", "content": text}],
            system=SYSTEM_FINANCE_PARSER,
            max_tokens=300
        )
        result_raw = result_raw.strip()
        if result_raw.startswith("```"):
            result_raw = result_raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = json.loads(result_raw)

        if not result.get("is_finance"):
            return None

        member_id = await ensure_member(user)
        category_id = await get_category_id(result.get("category_hint", "lainnya"))

        amount = result["amount"]
        if result["type"] == "expense":
            amount = -abs(amount)

        tx = await db.insert("finance_transactions", {
            "amount": amount,
            "type": result["type"],
            "category_id": category_id,
            "description": result.get("description", ""),
            "merchant": result.get("merchant", ""),
            "payment_method": result.get("payment_method", ""),
            "recorded_by": member_id,
            "source": "bot_chat",
        })

        if tx:
            abs_amount = abs(result["amount"])
            emoji = "💸" if result["type"] == "expense" else "💰"
            formatted = f"Rp {abs_amount:,.0f}".replace(",", ".")
            cat_name = CATEGORY_MAP.get(result.get("category_hint", ""), "Lainnya")

            await log_activity(chat_id, user.id, user.first_name, "finance_logged", {
                "amount": amount, "description": result.get("description", ""), "category": cat_name
            }, "finance_transactions", tx["id"])

            merchant_line = f"Tempat: {result['merchant']}\n" if result.get("merchant") else ""
            return (
                f"{emoji} *Tercatat!*\n\n"
                f"{'Pengeluaran' if result['type'] == 'expense' else 'Pemasukan'}: *{formatted}*\n"
                f"Kategori: {cat_name}\n"
                f"Keterangan: {result.get('description', '-')}\n"
                f"{merchant_line}\n"
                f"🦆 _Mau catat lagi atau tanya sesuatu?_"
            )
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.debug(f"Not a finance message: {e}")
        return None
    except Exception as e:
        logger.error(f"Finance parse error: {e}")
        return None


# ============================================
# ACTIVITY LOGGER
# ============================================
async def log_activity(chat_id, user_tg_id, user_name, action_type, detail=None, related_table=None, related_id=None):
    if not db:
        return
    await db.insert("bot_activity_log", {
        "chat_id": chat_id,
        "user_telegram_id": user_tg_id,
        "user_name": user_name or "",
        "action_type": action_type,
        "action_detail": json.dumps(detail) if detail else None,
        "related_table": related_table,
        "related_id": str(related_id) if related_id else None,
    })


# ============================================
# SAVE STORY TO SUPABASE
# ============================================
async def save_story(chat_id, chat_title, author_name, author_tg_id,
                     photo_count, story_text, interview_qa, member_id=None) -> dict | None:
    if not db:
        return None

    wib = datetime.now(timezone(timedelta(hours=7)))
    story_code = f"KWEK-{wib.strftime('%Y%m%d-%H%M%S')}"

    title = ""
    match = re.search(r'\*([^*]+)\*', story_text)
    if match:
        title = match.group(1).strip()
    else:
        title = story_text.split("\n")[0][:100].strip()

    tags = re.findall(r'#(\w+)', story_text)

    story = await db.insert("stories", {
        "story_code": story_code,
        "chat_id": chat_id,
        "chat_title": chat_title or "",
        "author_id": member_id,
        "author_telegram_id": author_tg_id,
        "author_name": author_name or "",
        "photo_count": photo_count,
        "title": title,
        "story_text": story_text,
        "interview_qa": interview_qa,
        "tags": tags,
    })

    if story:
        logger.info(f"✅ Cerita tersimpan: {story_code}")
        await log_activity(chat_id, author_tg_id, author_name, "story_created", {
            "story_code": story_code, "title": title, "photo_count": photo_count
        }, "stories", story["id"])

    return story


# ============================================
# SESSION MANAGEMENT
# ============================================
def get_sess(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = {
            "mode": "idle", "photos": [], "history": [],
            "q_count": 0, "general": [],
            "story_author": None, "story_author_id": None, "story_member_uuid": None,
        }
    return sessions[chat_id]


def reset_sess(chat_id):
    sessions[chat_id] = {
        "mode": "idle", "photos": [], "history": [],
        "q_count": 0, "general": [],
        "story_author": None, "story_author_id": None, "story_member_uuid": None,
    }


# ============================================
# COMMAND HANDLERS
# ============================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await ensure_member(update.effective_user)
    await update.message.reply_text(
        "🦆 *Kwek datang!*\n\n"
        "Halo keluarga! Aku Kwek, teman setia kalian.\n\n"
        "Yang bisa aku lakukan:\n"
        "📸 *Kirim foto* → Aku buatkan cerita indah\n"
        "💬 *Tanya apapun* → Aku jawab seperti asisten pribadi\n"
        "💰 *Catat pengeluaran* → \"beli makan 35rb di warteg\"\n"
        "📖 /cerita → Mulai sesi buat cerita\n"
        "💰 /keuangan → Lihat ringkasan keuangan\n"
        "📊 /status → Cek status database\n"
        "❓ /help → Panduan lengkap\n\n"
        "Kwek siap! 🦆✨",
        parse_mode="Markdown"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🦆 *Panduan Kwek*\n\n"
        "*📸 Buat Cerita dari Foto:*\n"
        "1. Kirim 2-8 foto ke sini\n"
        "2. Kwek analisis & tanya beberapa hal\n"
        "3. Jawab santai → cerita jadi!\n\n"
        "*💰 Catat Keuangan:*\n"
        "Langsung ketik natural:\n"
        "\"beli bensin 50rb\" → otomatis tercatat\n"
        "\"gaji masuk 5jt\" → otomatis tercatat\n\n"
        "*💬 Asisten Umum:*\n"
        "Langsung tanya saja!\n\n"
        "*⌨️ Commands:*\n"
        "/cerita - Mulai sesi cerita\n"
        "/selesai - Foto sudah semua\n"
        "/batal - Batalkan sesi\n"
        "/keuangan - Ringkasan keuangan\n"
        "/status - Cek status database\n"
        "/help - Panduan ini",
        parse_mode="Markdown"
    )


async def cmd_cerita(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reset_sess(chat_id)
    sess = get_sess(chat_id)
    sess["mode"] = "collecting"
    sess["story_author"] = update.effective_user.first_name
    sess["story_author_id"] = update.effective_user.id
    sess["story_member_uuid"] = await ensure_member(update.effective_user)
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


async def cmd_keuangan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not db:
        await update.message.reply_text("🦆 Database belum terhubung!")
        return

    now = datetime.now(timezone(timedelta(hours=7)))
    month_start = now.strftime("%Y-%m-01")

    txs = await db.select(
        "finance_transactions",
        f"transaction_date=gte.{month_start}&order=transaction_date.desc",
        limit=100
    )

    if not txs:
        await update.message.reply_text(
            f"🦆 Belum ada catatan keuangan di bulan {now.strftime('%B %Y')}.\n\n"
            "Mulai catat dengan ketik langsung, contoh:\n"
            "\"beli makan 25rb di warteg\"\n"
            "\"bayar listrik 350rb\""
        )
        return

    total_expense = sum(abs(t["amount"]) for t in txs if t["type"] == "expense")
    total_income = sum(t["amount"] for t in txs if t["type"] == "income")
    tx_count = len(txs)

    fmt_exp = f"Rp {total_expense:,.0f}".replace(",", ".")
    fmt_inc = f"Rp {total_income:,.0f}".replace(",", ".")
    fmt_net = f"Rp {total_income - total_expense:,.0f}".replace(",", ".")

    recent = txs[:5]
    recent_lines = []
    for t in recent:
        amt = abs(t["amount"])
        emoji = "🔴" if t["type"] == "expense" else "🟢"
        recent_lines.append(f"{emoji} Rp {amt:,.0f}".replace(",", ".") + f" — {t.get('description', '-')}")

    await update.message.reply_text(
        f"💰 *Keuangan {now.strftime('%B %Y')}*\n\n"
        f"📊 Total {tx_count} transaksi\n"
        f"🟢 Pemasukan: *{fmt_inc}*\n"
        f"🔴 Pengeluaran: *{fmt_exp}*\n"
        f"📍 Saldo bersih: *{fmt_net}*\n\n"
        f"*5 Transaksi Terakhir:*\n" + "\n".join(recent_lines) + "\n\n"
        f"🦆 _Ketik pengeluaran/pemasukan kapan saja!_",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not db:
        await update.message.reply_text(
            "🦆 Database belum terhubung.\n"
            "Admin perlu setup SUPABASE_URL dan SUPABASE_KEY di Railway!"
        )
        return
    try:
        members = await db.select("family_members", "is_active=eq.true", limit=50)
        stories = await db.select("stories", "order=created_at.desc", limit=1)
        txs = await db.select("finance_transactions", "order=created_at.desc", limit=1)

        story_info = f"📖 Cerita: {stories[0]['title'][:40]}..." if stories else "📖 Belum ada cerita"
        tx_info = "💰 Keuangan aktif" if txs else "💰 Belum ada transaksi"

        await update.message.reply_text(
            "🦆 *Status Kwek Family Hub*\n\n"
            f"✅ Database: Terhubung\n"
            f"👨‍👩‍👧‍👦 Anggota: {len(members)} orang\n"
            f"{story_info}\n"
            f"{tx_info}\n\n"
            "Semua sistem berjalan normal! 🟢",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Status check error: {e}")
        await update.message.reply_text("🦆 Ada masalah koneksi ke database. Coba lagi nanti ya!")


# ============================================
# PHOTO & TEXT HANDLERS
# ============================================
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_sess(chat_id)
    if sess["mode"] == "idle":
        sess["mode"] = "collecting"
        sess["story_author"] = update.effective_user.first_name
        sess["story_author_id"] = update.effective_user.id
        sess["story_member_uuid"] = await ensure_member(update.effective_user)
        await update.message.reply_text(
            "🦆 Ada foto nih! Kirim semua foto momennya dulu.\n"
            "Kalau sudah semua, ketik */selesai* ya!",
            parse_mode="Markdown"
        )
    if sess["mode"] != "collecting":
        return
    if len(sess["photos"]) >= 8:
        await update.message.reply_text("🦆 Sudah 8 foto, cukup! Ketik /selesai untuk lanjut.")
        return
    photo_file = await ctx.bot.get_file(update.message.photo[-1].file_id)
    async with httpx.AsyncClient() as client:
        r = await client.get(photo_file.file_path)
        b64 = base64.b64encode(r.content).decode()
    sess["photos"].append(b64)
    n = len(sess["photos"])
    if n == 1:
        await update.message.reply_text("🦆 Foto 1 masuk! Kirim lagi ya (min. 2 foto).")
    elif n < 8:
        await update.message.reply_text(
            f"🦆 Foto {n} masuk ✅\nKirim lagi atau */selesai* kalau sudah semua.",
            parse_mode="Markdown"
        )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_sess(chat_id)
    text = update.message.text.strip()
    user = update.effective_user

    # MODE: Interviewing for story
    if sess["mode"] == "interviewing":
        sess["history"].append({"role": "user", "content": text})
        MAX_Q = 4
        if sess["q_count"] >= MAX_Q:
            await update.message.reply_text("🦆 Oke! Sekarang aku buatkan ceritanya... ✨")
            await generate_story(update, ctx, sess)
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

    # MODE: Normal chat — try finance first, then general
    try:
        await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")

        if db:
            finance_reply = await try_parse_finance(text, user, chat_id)
            if finance_reply:
                await update.message.reply_text(finance_reply, parse_mode="Markdown")
                return

        sess["general"].append({"role": "user", "content": text})
        if len(sess["general"]) > 12:
            sess["general"] = sess["general"][-12:]
        reply = await call_claude(messages=sess["general"], system=SYSTEM_KWEK)
        sess["general"].append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("🦆 Kwak! Ada gangguan sebentar. Coba lagi ya!")


# ============================================
# STORY GENERATION + SAVE
# ============================================
async def generate_story(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sess: dict):
    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or "Private Chat"

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

        await update.message.reply_text(
            f"📖 *CERITA KALIAN SUDAH JADI!*\n\n{story}",
            parse_mode="Markdown"
        )

        saved = await save_story(
            chat_id=chat_id,
            chat_title=chat_title,
            author_name=sess.get("story_author", ""),
            author_tg_id=sess.get("story_author_id", 0),
            photo_count=len(sess["photos"]),
            story_text=story,
            interview_qa=qa,
            member_id=sess.get("story_member_uuid"),
        )

        if saved:
            await update.message.reply_text(
                f"🦆 Cerita tersimpan permanen! 💾\n"
                f"ID: `{saved.get('story_code', '')}`\n\n"
                "Semoga jadi kenangan indah keluarga kalian!\n"
                "Ketik /cerita untuk buat cerita baru 💛",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "🦆 Semoga jadi kenangan indah keluarga kalian!\n"
                "⚠️ _Cerita belum tersimpan ke database._\n\n"
                "Ketik /cerita untuk buat cerita baru 💛",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("🦆 Aduh gagal buat cerita! Coba /cerita lagi ya.")


# ============================================
# MAIN
# ============================================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cerita", cmd_cerita))
    app.add_handler(CommandHandler("selesai", cmd_selesai))
    app.add_handler(CommandHandler("batal", cmd_batal))
    app.add_handler(CommandHandler("keuangan", cmd_keuangan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    db_status = "✅ Supabase" if db else "⚠️ No DB"
    logger.info(f"🦆 Kwek Bot is running! ({db_status})")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
