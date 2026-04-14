"""
Sharp Props Telegram Bot
Manages Telegram messages for sharp props alerts
"""
import asyncio
import os
import requests
from typing import List, Optional
from dotenv import load_dotenv
from bookiebeats_monitor import BookieBeatsAlert

# Load environment variables
load_dotenv()

# Telegram bot configuration
SHARP_PROPS_TELEGRAM_BOT_TOKEN = os.getenv('SHARP_PROPS_TELEGRAM_BOT_TOKEN', '')
SHARP_PROPS_TELEGRAM_CHAT_ID = os.getenv('SHARP_PROPS_TELEGRAM_CHAT_ID', '')

# Global state
_current_message_id = None  # Track the current message ID so we can edit/delete it


def format_alert_for_telegram(alert: BookieBeatsAlert) -> str:
    """Format a single alert for Telegram display"""
    teams = alert.teams or "Unknown"
    pick = alert.pick or ""
    qualifier = alert.qualifier or ""
    odds = alert.odds or "N/A"
    ev_percent = getattr(alert, 'ev_percent', 0.0)
    roi_percent = getattr(alert, 'roi_percent', 0.0)
    
    # Format: Team @ Team - Player Prop | EV% | Odds
    line_str = f" {qualifier}" if qualifier else ""
    ev_str = f"{ev_percent:.1f}%" if ev_percent > 0 else f"{roi_percent:.1f}%"
    
    return f"• {teams}\n  {pick}{line_str} | {ev_str} | {odds}"


def format_alerts_message(alerts: List[BookieBeatsAlert]) -> Optional[str]:
    """Format all alerts into a single Telegram message"""
    if not alerts:
        return None  # No message if no alerts
    
    header = "🎯 *Sharp Props (Pregame)*\n\n"
    
    lines = []
    for alert in alerts:
        formatted = format_alert_for_telegram(alert)
        lines.append(formatted)
    
    message = header + "\n".join(lines)
    return message


async def send_telegram_message(text: str, parse_mode: str = "Markdown") -> Optional[int]:
    """Send a Telegram message and return the message ID"""
    global _current_message_id
    
    if not SHARP_PROPS_TELEGRAM_BOT_TOKEN or not SHARP_PROPS_TELEGRAM_CHAT_ID:
        print("[SHARP PROPS BOT] WARNING: Telegram bot token or chat ID not configured")
        return None
    
    try:
        url = f"https://api.telegram.org/bot{SHARP_PROPS_TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            'chat_id': SHARP_PROPS_TELEGRAM_CHAT_ID,
            'text': text,
            'parse_mode': parse_mode
        }
        
        response = requests.post(url, json=data, timeout=5)
        response.raise_for_status()
        
        result = response.json()
        if result.get('ok'):
            message_id = result.get('result', {}).get('message_id')
            _current_message_id = message_id
            return message_id
        else:
            print(f"[SHARP PROPS BOT] Error sending message: {result}")
            return None
    except Exception as e:
        print(f"[SHARP PROPS BOT] Error sending Telegram message: {e}")
        return None


async def edit_telegram_message(message_id: int, text: str, parse_mode: str = "Markdown") -> bool:
    """Edit an existing Telegram message"""
    if not SHARP_PROPS_TELEGRAM_BOT_TOKEN or not SHARP_PROPS_TELEGRAM_CHAT_ID:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{SHARP_PROPS_TELEGRAM_BOT_TOKEN}/editMessageText"
        data = {
            'chat_id': SHARP_PROPS_TELEGRAM_CHAT_ID,
            'message_id': message_id,
            'text': text,
            'parse_mode': parse_mode
        }
        
        response = requests.post(url, json=data, timeout=5)
        response.raise_for_status()
        
        result = response.json()
        return result.get('ok', False)
    except Exception as e:
        # If edit fails (e.g., message was deleted), try sending a new one
        print(f"[SHARP PROPS BOT] Error editing message: {e}")
        return False


async def delete_telegram_message(message_id: int) -> bool:
    """Delete a Telegram message"""
    if not SHARP_PROPS_TELEGRAM_BOT_TOKEN or not SHARP_PROPS_TELEGRAM_CHAT_ID:
        return False
    
    try:
        url = f"https://api.telegram.org/bot{SHARP_PROPS_TELEGRAM_BOT_TOKEN}/deleteMessage"
        data = {
            'chat_id': SHARP_PROPS_TELEGRAM_CHAT_ID,
            'message_id': message_id
        }
        
        response = requests.post(url, json=data, timeout=5)
        response.raise_for_status()
        
        result = response.json()
        return result.get('ok', False)
    except Exception as e:
        print(f"[SHARP PROPS BOT] Error deleting message: {e}")
        return False


async def update_telegram_dashboard(alerts: List[BookieBeatsAlert]):
    """Update the Telegram dashboard with current alerts"""
    global _current_message_id
    
    # Format message
    message_text = format_alerts_message(alerts)
    
    # If no alerts, delete the message if it exists
    if not message_text:
        if _current_message_id:
            await delete_telegram_message(_current_message_id)
            _current_message_id = None
        return
    
    # If we have a message ID, try to edit it
    if _current_message_id:
        success = await edit_telegram_message(_current_message_id, message_text)
        if not success:
            # Edit failed (message might have been deleted), send new one
            new_id = await send_telegram_message(message_text)
            if new_id:
                _current_message_id = new_id
    else:
        # No existing message, send a new one
        new_id = await send_telegram_message(message_text)
        if new_id:
            _current_message_id = new_id

