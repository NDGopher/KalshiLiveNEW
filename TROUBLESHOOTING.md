# 🔧 Troubleshooting Guide - On The Go

## 🚨 Quick Fixes from iPhone

### Bot Not Running?

**Option 1: Via Dashboard**
1. Go to: http://142.93.176.166:5000/control
2. Check status
3. If stopped, restart via SSH (Termius)

**Option 2: Via SSH (Termius App)**
```bash
# Connect to server
ssh root@142.93.176.166

# Check status
systemctl status kalshi-bot.service

# Restart
systemctl restart kalshi-bot.service

# Check if it started
systemctl status kalshi-bot.service
```

---

### Can't Access Dashboard?

**Check 1: Bot Running?**
- SSH: `systemctl status kalshi-bot.service`
- Should show: `Active: active (running)`

**Check 2: Firewall?**
- SSH: `ufw status`
- Should show: `5000/tcp ALLOW`

**Check 3: Port Listening?**
- SSH: `ss -tulpn | grep 5000`
- Should show: `0.0.0.0:5000`

**Fix:**
```bash
# Restart bot
systemctl restart kalshi-bot.service

# Check firewall
ufw allow 5000/tcp
ufw reload
```

---

### Odds-API errors or no alerts?

**Check:**
- `ODDS_API_KEY` is set in `.env` and the dashboard was restarted after changes.
- `ODDS_POLL_INTERVAL_SECONDS` matches your plan (avoid rate limits).
- Logs for HTTP 401/429 from Odds-API.io — fix key or slow polling.

---

### Logs Show Errors?

**View Logs:**
- Web: http://142.93.176.166:5000/logs
- SSH: `journalctl -u kalshi-bot.service -f`

**Common Errors:**

**"Missing private key"**
- Normal - Kalshi WebSocket warning, can be ignored

**"Cannot connect to Kalshi"**
- Check internet connection
- Check Kalshi API status
- Restart bot: `systemctl restart kalshi-bot.service`

**"Module not found"**
- Dependencies missing
- SSH: `cd ~/BBKalshiLive && source venv/bin/activate && pip install -r requirements.txt`

---

### Bot Keeps Crashing?

**Check Logs:**
```bash
journalctl -u kalshi-bot.service -n 50
```

**Common Causes:**
1. **Token expired** - Update token
2. **Missing dependencies** - Reinstall: `pip install -r requirements.txt`
3. **Python errors** - Check logs for traceback

**Fix:**
```bash
# Update code
cd ~/BBKalshiLive
git pull

# Reinstall dependencies
source venv/bin/activate
pip install -r requirements.txt

# Restart
systemctl restart kalshi-bot.service
```

---

### Dashboard Shows "No Logs Available"?

**Check:**
1. Bot is running: `systemctl status kalshi-bot.service`
2. Logs exist: `journalctl -u kalshi-bot.service -n 10`

**Fix:**
- Restart bot: `systemctl restart kalshi-bot.service`
- Wait 30 seconds
- Refresh logs page

---

### Can't Update Token?

**Check:**
1. Bot is running (needs to be running for web interface)
2. You're logged in (username/password prompt)
3. Token is full (very long, starts with "eyJ")

**Fix:**
- If bot not running, restart it first
- Make sure you copied FULL token
- Don't include "Bearer " prefix

---

### Telegram Not Working?

**Check:**
1. `.env` has `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
2. Bot token is valid
3. Chat ID is correct

**Test:**
```bash
# SSH into server
cd ~/BBKalshiLive
source venv/bin/activate
python -c "from dashboard import send_telegram_message; send_telegram_message('Test message')"
```

---

## 📱 iPhone-Specific Issues

### Can't Connect via Termius?

**Check:**
1. Server IP is correct: `142.93.176.166`
2. Username: `root`
3. Password is correct
4. Internet connection is working

**Fix:**
- Try from different network (cellular vs WiFi)
- Check if server is running (ping `142.93.176.166`)

---

### Proxyman Not Capturing Traffic?

**Check:**
1. Certificate is installed and trusted
2. Proxy is enabled in Proxyman
3. Safari is using the proxy

**Fix:**
- Reinstall certificate
- Restart Proxyman
- Make sure proxy is "ON" in Proxyman

---

## 🔍 Diagnostic Commands (SSH)

**Check Everything:**
```bash
# Bot status
systemctl status kalshi-bot.service

# Recent logs
journalctl -u kalshi-bot.service -n 30

# Port listening
ss -tulpn | grep 5000

# Firewall
ufw status

# Disk space
df -h

# Memory
free -h

# Python version
python3 --version

# Dependencies
cd ~/BBKalshiLive && source venv/bin/activate && pip list
```

---

## 🆘 Emergency Procedures

### Bot Completely Broken?

**Full Reset:**
```bash
# Stop bot
systemctl stop kalshi-bot.service

# Update code
cd ~/BBKalshiLive
git pull

# Reinstall dependencies
source venv/bin/activate
pip install -r requirements.txt

# Check .env file
nano .env  # Make sure all values are set

# Restart
systemctl start kalshi-bot.service
systemctl status kalshi-bot.service
```

---

### Server Down?

**Check DigitalOcean:**
1. Go to DigitalOcean dashboard
2. Check droplet status
3. If stopped, power it on
4. Wait 2 minutes
5. Try accessing dashboard

---

## 📞 Still Having Issues?

1. **Check logs:** http://142.93.176.166:5000/logs
2. **Check control panel:** http://142.93.176.166:5000/control
3. **SSH and run diagnostics:** See commands above
4. **Check .env file:** Make sure all credentials are correct

---

**Most issues are fixed by:**
1. Restarting bot: `systemctl restart kalshi-bot.service`
2. Updating token if expired
3. Checking logs for specific errors

