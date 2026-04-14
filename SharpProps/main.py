"""
Sharp Props Main
Runs the sharp props monitor and Telegram bot
"""
import asyncio
import os
import sys
from dotenv import load_dotenv
from sharp_props_monitor import SharpPropsMonitor
from sharp_props_bot import update_telegram_dashboard

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, parent_dir)

# Load environment variables (try parent directory first, then current)
load_dotenv(dotenv_path=os.path.join(parent_dir, '.env'))
load_dotenv()  # Also try current directory

# Configuration
BOOKIEBEATS_TOKEN = os.getenv('BOOKIEBEATS_TOKEN', '')
SHARP_PROPS_TELEGRAM_BOT_TOKEN = os.getenv('SHARP_PROPS_TELEGRAM_BOT_TOKEN', '')
SHARP_PROPS_TELEGRAM_CHAT_ID = os.getenv('SHARP_PROPS_TELEGRAM_CHAT_ID', '')


async def main():
    """Main function"""
    print("=" * 80)
    print("Sharp Props Monitor - Starting...")
    print("=" * 80)
    
    # Check configuration
    if not BOOKIEBEATS_TOKEN:
        print("ERROR: BOOKIEBEATS_TOKEN not set in .env file")
        return
    
    if not SHARP_PROPS_TELEGRAM_BOT_TOKEN or not SHARP_PROPS_TELEGRAM_CHAT_ID:
        print("WARNING: SHARP_PROPS_TELEGRAM_BOT_TOKEN or SHARP_PROPS_TELEGRAM_CHAT_ID not set")
        print("Telegram alerts will be disabled")
    
    # Create monitor
    monitor = SharpPropsMonitor(auth_token=BOOKIEBEATS_TOKEN)
    
    # Add callback to update Telegram dashboard
    monitor.add_alert_callback(update_telegram_dashboard)
    
    # Start monitoring
    try:
        await monitor.start()
    except KeyboardInterrupt:
        print("\n[SHARP PROPS] Shutting down...")
        await monitor.stop()
    except Exception as e:
        print(f"[SHARP PROPS] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        await monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())

