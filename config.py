import os

# Replace with your actual Telegram Bot Token from BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# SQLite Database Name
DB_NAME = "stock_alerts.db"

# Interval to check stock (in seconds) - Default: 5 minutes
CHECK_INTERVAL = 300 
 
