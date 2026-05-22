"""
CalmMart Ltd — Telegram Web App Bot
- Single instance lock (prevents duplicate bot processes)
- Launches the Mini App inside Telegram
- Referrals: unlimited, minimum 80 encouraged
"""

import os
import sys
import logging
import fcntl
import atexit
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import Conflict
from database import Database
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.getenv("BOT_TOKEN")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://yourdomain.com")
LOCK_FILE  = "/tmp/calmmart_bot.lock"

db = Database()


# ── Single Instance Lock ───────────────────────────────────────
def acquire_lock():
    """
    Prevents more than one bot process from running at the same time.
    Uses a file lock so any second instance exits immediately.
    """
    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()

        # Release lock when process exits
        def release():
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
                os.remove(LOCK_FILE)
            except Exception:
                pass

        atexit.register(release)
        logger.info(f"✅ Bot instance lock acquired (PID {os.getpid()})")
        return lock_fd

    except BlockingIOError:
        logger.error("❌ Another bot instance is already running. Exiting.")
        sys.exit(1)


# ── Handlers ───────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    ref  = args[0] if args else None

    webapp_url = f"{WEBAPP_URL}?ref={ref}" if ref else WEBAPP_URL

    keyboard = [[
        InlineKeyboardButton(
            "🚀 Open CalmMart App",
            web_app=WebAppInfo(url=webapp_url)
        )
    ]]

    existing = db.get_user(user.id)
    greeting = "Welcome back" if existing else "Welcome"

    await update.message.reply_text(
        f"👋 {greeting}, {user.first_name}!\n\n"
        f"🏢 *CalmMart Ltd* — Nigeria's fastest-growing referral network.\n\n"
        f"💰 Activation Fee: ₦10,000\n"
        f"📈 Earn from unlimited referrals across 3 generations:\n"
        f"  • Gen 1: 15% — ₦1,500 each\n"
        f"  • Gen 2: 10% — ₦1,000 each\n"
        f"  • Gen 3:  5% —   ₦500 each\n\n"
        f"📅 Withdrawals every Wednesday & Friday\n"
        f"💵 Minimum withdrawal: ₦10,000\n\n"
        f"Tap below to open the app 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def set_menu_button(app: Application):
    """Sets the persistent bottom button to open the Web App."""
    try:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Open App",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
        logger.info("✅ Menu button set")
    except Exception as e:
        logger.warning(f"Menu button error: {e}")


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives any data sent from Web App via Telegram.sendData()"""
    data = update.effective_message.web_app_data.data
    logger.info(f"WebApp data: {data}")


async def referral_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = db.get_user(update.effective_user.id)
    if not user_data:
        await update.message.reply_text(
            "You are not registered yet. Open the CalmMart app to get started."
        )
        return
    stats = db.get_user_stats(user_data["referral_code"])
    total = stats["gen1"] + stats["gen2"] + stats["gen3"]
    await update.message.reply_text(
        f"📊 *Your Network*\n\n"
        f"Gen 1: {stats['gen1']} members → ₦{stats['gen1']*1500:,}\n"
        f"Gen 2: {stats['gen2']} members → ₦{stats['gen2']*1000:,}\n"
        f"Gen 3: {stats['gen3']} members → ₦{stats['gen3']*500:,}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"Total network: {total} members\n"
        f"Total earned: ₦{user_data['total_earnings']:,.0f}\n"
        f"Balance: ₦{user_data['wallet_balance']:,.0f}",
        parse_mode="Markdown"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle Conflict errors from duplicate bot instances."""
    if isinstance(context.error, Conflict):
        logger.error("❌ Conflict: Another bot instance is running. Shutting down.")
        sys.exit(1)
    logger.error(f"Update error: {context.error}")


# ── Main ───────────────────────────────────────────────────────
def main():
    # Prevent duplicate instances
    acquire_lock()

    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set in environment variables.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", referral_stats))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_error_handler(error_handler)

    # Set menu button on startup
    app.post_init = set_menu_button

    logger.info("🚀 CalmMart Bot starting (single instance mode)...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,    # ignore old queued updates on restart
        close_loop=False,
    )


if __name__ == "__main__":
    main()
