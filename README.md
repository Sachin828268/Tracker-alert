# 📦 Telegram Stock Alert Bot

A Telegram bot that monitors product pages on **Amazon**, **Flipkart**, **Zepto**, and **BigBasket** and sends you an instant alert when an out-of-stock item becomes available.

---

## Features

| Feature | Details |
|---------|---------|
| 🔍 Auto site detection | Paste any supported URL — the bot detects the site automatically |
| 🛒 Multi-site support | Amazon · Flipkart · Zepto · BigBasket |
| 🔔 Instant alerts | Notified the moment stock status flips from ❌ → ✅ |
| 👤 Per-user tracking | Each user manages their own product list |
| 🗄️ Persistent storage | SQLite — no external database needed |
| 🎭 Playwright scraping | Handles JavaScript-heavy pages reliably |

---

## Project Structure

```
telegram-stock-bot/
├── bot.py            # Entry point + background stock-check loop
├── handlers.py       # /start /add /list /remove command handlers
├── database.py       # SQLite CRUD operations
├── stock_checker.py  # Playwright scrapers (one per site)
├── states.py         # aiogram FSM states
├── config.py         # Environment-based configuration
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/yourname/telegram-stock-bot.git
cd telegram-stock-bot

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

### 2. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env`:

```env
BOT_TOKEN=your_telegram_bot_token_here
DB_PATH=stock_alerts.db
CHECK_INTERVAL=300          # How often to check (seconds). Default: 5 min
```

Get your bot token from [@BotFather](https://t.me/BotFather) on Telegram.

### 3. Run the bot

```bash
python bot.py
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message & command overview |
| `/add` | Start tracking a new product (guided 2-step flow) |
| `/list` | View all your tracked products with current stock status |
| `/remove` | Stop tracking a product (tap from inline keyboard) |
| `/cancel` | Cancel the current add-product flow |

### Example: Adding a product

```
You:  /add
Bot:  Send me the product name.

You:  Amul Butter 500g
Bot:  Now send me the product URL.

You:  https://www.bigbasket.com/pd/10000051/amul-butter-500-g/
Bot:  ✅ Product added! I'll notify you when it's back in stock.
```

---

## How Stock Detection Works

Each site has a dedicated checker in `stock_checker.py`:

### Amazon
Looks for `#add-to-cart-button` or `#buy-now-button`. Falls back to reading the `#availability` span text.

### Flipkart
Detects "Add to Cart" / "Buy Now" button classes. Checks for Flipkart's known out-of-stock class (`._16FRp0`).

### Zepto
Waits for `networkidle`, then looks for an "Add" button. Falls back to scanning body text.

### BigBasket
Looks for "Add" button. A "Notify Me" button is treated as out-of-stock.

---

## Architecture

```
┌─────────────┐     /add /list /remove      ┌──────────────┐
│   Telegram  │ ◄──────────────────────────► │  handlers.py │
│    User     │                              │  (aiogram)   │
└─────────────┘                              └──────┬───────┘
                                                    │
                                             ┌──────▼───────┐
                                             │  database.py  │
                                             │   (SQLite)    │
                                             └──────┬───────┘
                                                    │ every N seconds
                                             ┌──────▼───────────┐
                                             │ stock_checker.py  │
                                             │  (Playwright)     │
                                             │                   │
                                             │  Amazon · Flipkart│
                                             │  Zepto · BigBasket│
                                             └──────────────────┘
```

The background loop in `bot.py` (`stock_checker_loop`) runs every `CHECK_INTERVAL` seconds. When it detects a product flip from out-of-stock → in-stock, it fires a Telegram message to the owning user.

---

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_TOKEN` | *(required)* | Telegram Bot API token |
| `DB_PATH` | `stock_alerts.db` | Path to the SQLite database file |
| `CHECK_INTERVAL` | `300` | Seconds between stock check cycles |

---

## Running as a Service (systemd)

```ini
# /etc/systemd/system/stock-bot.service
[Unit]
Description=Telegram Stock Alert Bot
After=network.target

[Service]
WorkingDirectory=/opt/telegram-stock-bot
ExecStart=/opt/telegram-stock-bot/venv/bin/python bot.py
Restart=always
EnvironmentFile=/opt/telegram-stock-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable stock-bot
sudo systemctl start stock-bot
sudo journalctl -u stock-bot -f
```

---

## Notes & Limitations

- **Rate limiting**: A 2-second delay is inserted between product checks to avoid being flagged as a bot. Reduce `CHECK_INTERVAL` cautiously.
- **Site changes**: E-commerce sites update their HTML/class names frequently. If detection breaks, update the selectors in `stock_checker.py`.
- **Zepto / BigBasket**: These sites are SPA-heavy. The `networkidle` wait strategy handles most cases, but occasionally a longer `PLAYWRIGHT_TIMEOUT` (in `config.py`) may be needed.
- **CAPTCHAs**: High-frequency checks may trigger CAPTCHA pages. Consider rotating user agents or adding proxy support for heavy usage.

---

## License

MIT — use freely, attribution appreciated.

