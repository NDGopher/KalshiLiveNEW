"""Script to adapt dashboard.py for Polymarket"""
import re

# Read the original dashboard
with open('../dashboard.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replacements
replacements = [
    ('KalshiClient', 'PolymarketClient'),
    ('kalshi_client', 'polymarket_client'),
    ('MarketMatcher', 'MarketMatcherPolymarket'),
    ('market_matcher', 'market_matcher_polymarket'),
    ('BookieBeatsAPIMonitor', 'BookieBeatsAPIMonitorPolymarket'),
    ('from kalshi_client import', 'from Polymarket.polymarket_client import'),
    ('from market_matcher import', 'from Polymarket.market_matcher_polymarket import'),
    ('from bookiebeats_api_monitor import', 'from Polymarket.bookiebeats_api_monitor_polymarket import'),
    ('Kalshi All Sports (3 Sharps Live)', 'Polymarket All Sports (3 Sharps Live)'),
    ('CBB EV Filter (Live - Kalshi)', 'CBB EV Filter (Live - Polymarket)'),
    ('"Kalshi"', '"Polymarket"'),
    ("'Kalshi'", "'Polymarket'"),
    ("['Kalshi']", "['Polymarket']"),
    ('kalshi-live-betting-secret-key', 'polymarket-live-betting-secret-key'),
    ('dashboard.html', 'dashboard_polymarket.html'),
    ('KALSHI LIVE BETTING SYSTEM', 'POLYMARKET LIVE BETTING SYSTEM'),
    ('Kalshi Bot Status', 'Polymarket Bot Status'),
    ('Kalshi client session', 'Polymarket client session'),
    # NHL ticker patterns - Polymarket may use different format, but keep logic
    # ('KXNHL', 'POLYNHL'),  # Don't replace - Polymarket may use different ticker format
]

# Apply replacements
for old, new in replacements:
    content = content.replace(old, new)

# Update filter payloads
content = re.sub(
    r'"bettingBooks":\s*\["Kalshi"\]',
    '"bettingBooks": ["Polymarket"]',
    content
)

content = re.sub(
    r'"displayBooks":\s*\["Kalshi"',
    '"displayBooks": ["Polymarket"',
    content
)

content = re.sub(
    r'"minLimits":\s*\[\{"book":\s*"Kalshi"',
    '"minLimits": [{"book": "Polymarket"',
    content
)

# Write adapted dashboard
with open('dashboard_polymarket_full.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Created dashboard_polymarket_full.py")
