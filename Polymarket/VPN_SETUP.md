# VPN/Proxy Setup for Polymarket

Since Polymarket is geoblocked in the US, you'll need to use a VPN or proxy to access the non-US site.

## ExpressVPN Setup (Recommended)

ExpressVPN provides local proxy servers that work well with this bot.

### Option 1: SOCKS5 Proxy (Recommended)

Set in `.env`:
```
POLYMARKET_SOCKS5=socks5://127.0.0.1:1080
```

Requires: `pip install aiohttp-socks`

**ExpressVPN SOCKS5 Setup:**
1. Install ExpressVPN
2. Connect to a non-US server (e.g., UK, Canada, Mexico)
3. Enable "Local Network" in ExpressVPN settings (if needed)
4. ExpressVPN typically uses port 1080 for SOCKS5
5. Set `POLYMARKET_SOCKS5=socks5://127.0.0.1:1080` in `.env`

### Option 2: HTTP Proxy

Set in `.env`:
```
POLYMARKET_PROXY=http://127.0.0.1:8080
```

**ExpressVPN HTTP Proxy Setup:**
1. Connect to ExpressVPN
2. ExpressVPN may provide HTTP proxy on port 8080
3. Set `POLYMARKET_PROXY=http://127.0.0.1:8080` in `.env`

### Option 3: System-Wide VPN

If you're using ExpressVPN's system-wide VPN connection, you may not need proxy settings - the bot will use the system's VPN connection automatically. However, for more reliable connections, we recommend using the SOCKS5 proxy option above.

## Option 3: System-Wide VPN

If you're using a system-wide VPN (like NordVPN app), you may not need proxy settings - the bot will use the system's VPN connection automatically.

## Testing Your Proxy

You can test if your proxy works by:

```python
import aiohttp
import asyncio

async def test():
    proxy = "http://127.0.0.1:8080"  # or socks5://...
    connector = aiohttp.TCPConnector()
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get("https://clob.polymarket.com/markets", proxy=proxy) as resp:
            print(f"Status: {resp.status}")
            print(await resp.text()[:200])

asyncio.run(test())
```

## Legal Notice

⚠️ **Important**: Using VPN to access geoblocked services may violate Terms of Service. Use at your own risk.
