#!/bin/bash
# Quick bot status check script
# Run this from your PC: bash check_bot_status.sh

SERVER="root@142.93.176.166"

echo "=========================================="
echo "KALSHI BOT STATUS CHECK"
echo "=========================================="
echo ""

echo "1. Checking if service is running..."
ssh $SERVER "systemctl is-active kalshi-bot.service" && echo "✅ Service is RUNNING" || echo "❌ Service is NOT running"
echo ""

echo "2. Checking service status..."
ssh $SERVER "systemctl status kalshi-bot.service --no-pager -l | head -15"
echo ""

echo "3. Checking recent logs (last 20 lines)..."
ssh $SERVER "journalctl -u kalshi-bot.service -n 20 --no-pager | tail -20"
echo ""

echo "4. Checking for errors in last 50 lines..."
ssh $SERVER "journalctl -u kalshi-bot.service -n 50 --no-pager | grep -i 'error\|failed\|expired\|exception' | tail -10"
echo ""

echo "5. Checking if monitors are running..."
ssh $SERVER "journalctl -u kalshi-bot.service -n 100 --no-pager | grep -i 'monitor loop\|polling every' | tail -5"
echo ""

echo "=========================================="
echo "Web Dashboard: http://142.93.176.166:5000"
echo "=========================================="

