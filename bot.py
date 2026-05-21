"""
CalmMart Ltd — Telegram Web App Bot
Launches the Mini App instead of chat-based registration.
Referrals: unlimited (no cap), minimum 80 per user encouraged.
"""

import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from database import Database
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(format="%(asctime)s — %(name)s — %(levelname)s — %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN   = os.getenv("BOT_TOKEN")
WEBAPP_URL  = os.getenv("WEBAPP_URL", "https://yourdomain.com")   # URL where index.html is served
db = Database()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    args  = context.args
    ref   = args[0] if args else None

    # Store referral code in deep-link so WebApp can read it
    webapp_url = f"{WEBAPP_URL}?ref={ref}" if ref else WEBAPP_URL

    keyboard = [[
        InlineKeyboardButton(
            "🚀 Open CalmMart App",
            web_app=WebAppInfo(url=webapp_url)
        )
    ]]

    await update.message.reply_text(
        f"👋 Welcome{' back' if db.get_user(user.id) else ''}, {user.first_name}!\n\n"
        f"🏢 *CalmMart Ltd* — Nigeria's fastest-growing referral network.\n\n"
        f"💰 Registration: ₦10,000\n"
        f"📈 Earn from unlimited referrals across 3 generations:\n"
        f"  • Gen 1: 15% (₦1,500 each)\n"
        f"  • Gen 2: 10% (₦1,000 each)\n"
        f"  • Gen 3:  5% (₦500 each)\n\n"
        f"Tap below to open the app 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def set_menu_button(app: Application):
    """Sets the persistent menu button to open the Web App."""
    await app.bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Open App", web_app=WebAppInfo(url=WEBAPP_URL))
    )


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives data sent from the Web App via Telegram.sendData()"""
    data = update.effective_message.web_app_data.data
    logger.info(f"WebApp data received: {data}")
    # Payment confirmation is handled via Paystack webhook,
    # but we can handle other app events here if needed.
    await update.message.reply_text("✅ Action received from app!")


async def referral_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = db.get_user(update.effective_user.id)
    if not user_data:
        await update.message.reply_text("Please open the CalmMart app to register first.")
        return
    stats = db.get_user_stats(user_data["referral_code"])
    total = stats["gen1"] + stats["gen2"] + stats["gen3"]
    await update.message.reply_text(
        f"📊 *Your Network*\n\n"
        f"Gen 1: {stats['gen1']} members → ₦{stats['gen1']*1500:,}\n"
        f"Gen 2: {stats['gen2']} members → ₦{stats['gen2']*1000:,}\n"
        f"Gen 3: {stats['gen3']} members → ₦{stats['gen3']*500:,}\n"
        f"━━━━━━━━━━━━\n"
        f"Total network: {total} members\n"
        f"Total earned: ₦{user_data['total_earnings']:,.0f}",
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", referral_stats))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))

    # Set persistent menu button on startup
    app.post_init = set_menu_button

    logger.info("CalmMart Web App Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
