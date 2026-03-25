import os
import logging
import base64
import httpx
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"

# Session storage per chat
sessions = {}

SYSTEM_KWEK = """Kamu adalah Kwek 🦆, asisten keluarga yang ceria, hangat, cerdas, dan sedikit lucu.
Kamu berbicara Bahasa Indonesia yang natural, santai, seperti teman dekat keluarga.
Gunakan emoji secukupnya. Jangan terlalu formal.

Kemampuanmu:
1. SUTRADARA CERITA: Saat ada foto, kamu interview keluarga lalu buatkan story indah
2. ASISTEN UMUM: Jawab pertanyaan apapun — pengetahuan umum, saran, ide, dll
3. PENGINGAT & CATATAN: Bantu catat hal penting yang disebutkan keluarga

Saat mode interview story:
- Analisis foto dengan detail dan empati
- Ajukan pertanyaan personal, spesifik, dan hangat — bukan generik
- Fokus pada emosi, detail unik, momen lucu atau mengharukan
- Maksimal 4 pertanyaan sebelum buat cerita"""

SYSTEM_STORY = """Kamu adalah penulis cerita keluarga yang puitis, hangat, dan berbakat.
Tulis cerita berdasarkan foto dan hasil interview.
Gunakan Bahasa Indonesia yang indah, personal, menyentuh — tidak berlebihan.
Cerita harus terasa seperti ditulis oleh seseorang yang benar-benar hadir di momen itu."""


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


def get_sess(chat_id):
    if chat_id not in sessions:
        sessions[chat_id] = {
            "mode": "idle",       # idle | collecting | interviewing
            "photos": [],         # base64 list
            "history": [],        # interview Q&A
            "q_count": 0,
            "general": []         # general chat history
        }
    return sessions[chat_id]


def reset_sess(chat_id):
    sessions[chat_id] = {
        "mode": "idle", "photos": [], "history": [],
        "q_count": 0, "general": []
    }


# ── COMMANDS ──

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🦆 *Kwek datang!*\n\n"
        "Halo keluarga! Aku Kwek, teman setia kalian.\n\n"
        "Yang bisa aku lakukan:\n"
        "📸 *Kirim foto* → Aku buatkan cerita indah\n"
        "💬 *Tanya apapun* → Aku jawab seperti asisten pribadi\n"
        "📖 /cerita → Mulai sesi buat cerita\n"
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
        "3. Jawab santai → cerita jadi!\n"
        "Atau ketik /cerita untuk mulai\n\n"
        "*💬 Asisten Umum:*\n"
        "Langsung tanya saja!\n"
        "Contoh: _'Kwek, resep nasi goreng dong'_\n\n"
        "*⌨️ Commands:*\n"
        "/cerita - Mulai sesi cerita\n"
        "/selesai - Foto sudah semua, mulai interview\n"
        "/batal - Batalkan sesi\n"
        "/start - Sapa Kwek\n"
        "/help - Panduan ini",
        parse_mode="Markdown"
    )


async def cmd_cerita(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reset_sess(chat_id)
    get_sess(chat_id)["mode"] = "collecting"
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
            messages=[{"role": "user", "content": f"Ini {n} foto dari momen keluarga. Analisis semua foto dan mulai interview dengan 1 pertanyaan pertama yang paling penting dan personal. Singkat, hangat, dan spesifik berdasarkan apa yang kamu lihat."}],
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


# ── PHOTO HANDLER ──

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_sess(chat_id)

    if sess["mode"] == "idle":
        sess["mode"] = "collecting"
        await update.message.reply_text(
            "🦆 Ada foto nih! Kirim semua foto momennya dulu.\n"
            "Kalau sudah semua, ketik */selesai* ya!",
            parse_mode="Markdown"
        )

    if sess["mode"] != "collecting":
        await update.message.reply_text("🦆 Lagi ada sesi lain nih. Ketik /batal dulu kalau mau mulai baru.")
        return

    if len(sess["photos"]) >= 8:
        await update.message.reply_text("🦆 Sudah 8 foto, cukup! Ketik /selesai untuk lanjut.")
        return

    # Download & encode
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


# ── TEXT HANDLER ──

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_sess(chat_id)
    text = update.message.text.strip()

    # ── INTERVIEW MODE ──
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
                system_add += " Ini pertanyaan TERAKHIR. Balas singkat lalu tutup dengan hangat bahwa kamu akan buatkan ceritanya."
            else:
                system_add += " Balas singkat (1 kalimat apresiasi) lalu ajukan pertanyaan berikutnya yang lebih dalam."

            reply = await call_claude(
                messages=sess["history"],
                system=SYSTEM_KWEK + system_add
            )
            sess["history"].append({"role": "assistant", "content": reply})
            sess["q_count"] += 1
            await update.message.reply_text(reply)

        except Exception as e:
            logger.error(e)
            await update.message.reply_text("🦆 Koneksi bermasalah, coba jawab lagi ya!")

    # ── GENERAL ASSISTANT MODE ──
    else:
        try:
            await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
            sess["general"].append({"role": "user", "content": text})
            if len(sess["general"]) > 12:
                sess["general"] = sess["general"][-12:]

            reply = await call_claude(messages=sess["general"], system=SYSTEM_KWEK)
            sess["general"].append({"role": "assistant", "content": reply})
            await update.message.reply_text(reply)

        except Exception as e:
            logger.error(e)
            await update.message.reply_text("🦆 Kwak! Ada gangguan sebentar. Coba lagi ya!")


# ── STORY GENERATOR ──

async def generate_story(update: Update, ctx: ContextTypes.DEFAULT_TYPE, sess: dict):
    qa = "\n".join([
        f"{'Keluarga' if m['role']=='user' else 'Kwek'}: {m['content']}"
        for m in sess["history"] if isinstance(m.get("content"), str)
    ])

    prompt = f"""Berdasarkan {len(sess['photos'])} foto dan hasil interview:

{qa}

Buatkan cerita keluarga yang indah. Gunakan format ini:

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
        await update.message.reply_text(
            "🦆 Semoga jadi kenangan indah keluarga kalian!\n"
            "Ketik /cerita untuk buat cerita baru, atau tanya apapun ke Kwek ya 💛"
        )
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("🦆 Aduh gagal buat cerita! Coba /cerita lagi ya.")


# ── MAIN ──

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cerita", cmd_cerita))
    app.add_handler(CommandHandler("selesai", cmd_selesai))
    app.add_handler(CommandHandler("batal", cmd_batal))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("🦆 Kwek Bot is running!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
