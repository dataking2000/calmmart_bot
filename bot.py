"""
CalmMart Ltd — Telegram Web App Bot
Uses WEBHOOK mode instead of polling — eliminates all Conflict errors.
Telegram pushes updates to the server; no duplicate instance issues.
"""

import os
import sys
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # e.g. https://calmmart.up.railway.app/webhook/paystack
# Bot webhook path — different from Paystack webhook
BOT_WEBHOOK_PATH = "/bot/webhook"
BOT_WEBHOOK_URL  = os.getenv("WEBAPP_URL", "").rstrip("/") + BOT_WEBHOOK_PATH

db = Database()


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


# ── Build app ──────────────────────────────────────────────────
def build_app() -> Application:
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", referral_stats))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.post_init = set_menu_button
    return app


# ── Main — webhook mode ────────────────────────────────────────
def main():
    app = build_app()

    logger.info(f"🚀 CalmMart Bot starting in WEBHOOK mode...")
    logger.info(f"📡 Webhook URL: {BOT_WEBHOOK_URL}")

    app.run_webhook(
        listen        = "0.0.0.0",
        port          = int(os.getenv("BOT_PORT", "8443")),
        webhook_url   = BOT_WEBHOOK_URL,
        url_path      = BOT_WEBHOOK_PATH,
        drop_pending_updates = True,
    )


if __name__ == "__main__":
    main()
