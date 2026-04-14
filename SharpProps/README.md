# Sharp Props Monitor

Separate system for monitoring sharp player props from BookieBeats lowHold API.

## Features

- Monitors pregame sharp player props
- Polls every 1 second (slower than main filter, no need for speed)
- Telegram dashboard that updates in real-time
- Only shows alerts when plays are active
- Sorted by highest EV/ROI at top

## Setup

1. **Create a separate Telegram bot** (optional but recommended):
   - Message @BotFather on Telegram
   - Create a new bot with `/newbot`
   - Save the bot token
   - Get your chat ID by messaging the bot and visiting: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`

2. **Add to your `.env` file** (same file as main dashboard, or create a separate one in SharpProps folder):
   ```
   BOOKIEBEATS_TOKEN=your_token_here
   SHARP_PROPS_TELEGRAM_BOT_TOKEN=your_telegram_bot_token
   SHARP_PROPS_TELEGRAM_CHAT_ID=your_telegram_chat_id
   ```
   
   **Note**: You can use the same `.env` file as the main dashboard, or create a separate one. The SharpProps system will look for `.env` in the parent directory (KalshiLiveBetting) or current directory.

3. **Run**:
   ```
   python main.py
   ```
   Or use `run.bat` on Windows.

## How It Works

- Monitors the `/v1/tools/lowHold` endpoint
- When alerts are active, sends/updates a single Telegram message
- When alerts clear, deletes the message
- Message is sorted by highest EV/ROI percentage

