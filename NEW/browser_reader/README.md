# Browser Reader - Safest Monitoring Method

This is a **drop-in replacement** for `BookieBeatsAPIMonitor` that connects to existing browser windows instead of making API calls.

## Why This is Safer

- ✅ **No API calls** - Just reads DOM from your browser
- ✅ **No automation flags** - Chrome doesn't know it's being controlled
- ✅ **Uses your login session** - No tokens needed
- ✅ **Completely undetectable** - Looks like normal browsing

## Setup

### 1. Start Chrome with Remote Debugging

**Windows:**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\temp\chrome-debug-1"
```

**For second browser (optional):**
```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9223 --user-data-dir="C:\temp\chrome-debug-2"
```

### 2. Open BookieBeats

1. Navigate to `https://www.bookiebeats.com/tools/live`
2. Log in normally (no VPN needed)
3. Set up your filters
4. Leave the window open

### 3. Use in Dashboard

Replace `BookieBeatsAPIMonitor` with `BookieBeatsBrowserReader`:

```python
# OLD:
from bookiebeats_api_monitor import BookieBeatsAPIMonitor
monitor = BookieBeatsAPIMonitor(auth_token=token)

# NEW:
from NEW.browser_reader.monitor import BookieBeatsBrowserReader
monitor = BookieBeatsBrowserReader(cdp_endpoints=['http://localhost:9222'])
```

## Interface Compatibility

This monitor has the **exact same interface** as `BookieBeatsAPIMonitor`:

- `set_filter(filter_payload)` - Sets filter (for compatibility, but doesn't use it)
- `add_alert_callback(callback)` - Callback for new alerts
- `add_removed_alert_callback(callback)` - Callback for removed alerts
- `add_updated_alert_callback(callback)` - Callback for updated alerts
- `start()` - Start monitoring
- `stop()` - Stop monitoring
- `poll_interval` - Polling interval (default 0.5s)

## Multiple Browsers

You can use multiple browser windows for redundancy:

```python
monitor = BookieBeatsBrowserReader(
    cdp_endpoints=['http://localhost:9222', 'http://localhost:9223']
)
```

## Troubleshooting

**"Failed to connect" error:**
- Make sure Chrome is running with `--remote-debugging-port=9222`
- Check firewall isn't blocking port 9222
- Try `http://localhost:9222/json` in browser - should show Chrome info

**"No pages found":**
- Make sure BookieBeats is open in the browser
- Check the URL contains "bookiebeats.com"

**Port already in use:**
- Use different ports: `--remote-debugging-port=9223`
- Or close other Chrome instances using that port
