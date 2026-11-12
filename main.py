import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

def shorten_address(address: str) -> str:
    """Truncates a wallet address to show only the first 4 and last 4 characters."""
    if not address or len(address) <= 8:
        return address
    return f"{address[:4]}...{address[-4:]}"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simulated wallet data
    wallet_address = "FMboEKMmAf3e6yKMdbs9oAKFpSaiTSh9Kf9iPqseZj9B"
    truncated_addr = shorten_address(wallet_address)
    
    wallet_label = "Wallet 1"
    balance = "469.3123 SOL"
    chain = "Solana"
    status = "Active"

    message_text = (
        f"✅ **Wallet Connected**\n\n"
        f"🔑 Address: `{truncated_addr}`\n"
        f"🏷️ Label: {wallet_label}\n"
        f"💰 Balance: {balance}\n"
        f"🔗 Chain: {chain}\n"
        f"📊 Status: {status}\n\n"
        f"Your wallet is ready. You can now deploy tokens."
    )

    # Inline keyboards matching the UI
    keyboard = [
        [InlineKeyboardButton("🚀 Launch", callback_data="launch"), InlineKeyboardButton("💼 Wallets", callback_data="wallets")],
        [InlineKeyboardButton("📊 Portfolio", callback_data="portfolio"), InlineKeyboardButton("📄 Transactions", callback_data="transactions")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings"), InlineKeyboardButton("🔑 Access", callback_data="access")],
        [InlineKeyboardButton("💰 Ops Pool", callback_data="ops_pool")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(message_text, parse_mode="Markdown", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(message_text, parse_mode="Markdown", reply_markup=reply_markup)

def main():
    # Replace 'YOUR_BOT_TOKEN' with your actual Telegram bot token
    application = ApplicationBuilder().token("YOUR_BOT_TOKEN").build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(start))

    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
