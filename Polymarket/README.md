# Polymarket Betting Bot

This is a port of the Kalshi betting bot for Polymarket. It uses the same BookieBeats API feeds but connects to Polymarket instead.

## Features

- **Lower Fees**: ~0.01% vs Kalshi's higher fees
- **Crypto-Based**: USDC, no KYC on non-US site
- **VPN Support**: Access non-US Polymarket site via proxy/VPN
- **Same Logic**: Reuses market matching, auto-betting, and dashboard logic
- **CSV Tracking**: Positions tracked locally in `positions.csv`

## Setup

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure VPN/Proxy** (if needed):
   - Set `POLYMARKET_PROXY` in `.env` (format: `http://proxy:port` or `socks5://proxy:port`)
   - Or use NordVPN/SOCKS5 proxy

3. **Set API Keys**:
   - `POLYMARKET_API_KEY`: Your Polymarket API key
   - `POLYMARKET_PRIVATE_KEY`: Your private key (if required)

4. **Run**:
   ```bash
   python main_polymarket.py
   ```

## VPN Configuration

The bot supports proxy/VPN via environment variables:
- `POLYMARKET_PROXY`: HTTP/HTTPS proxy (e.g., `http://127.0.0.1:8080`)
- `POLYMARKET_SOCKS5`: SOCKS5 proxy (e.g., `socks5://127.0.0.1:1080`)

For NordVPN, you can:
1. Use NordVPN's SOCKS5 proxy feature
2. Set up a local proxy server
3. Use a VPN service that provides HTTP proxy

## Differences from Kalshi

- **API Structure**: Polymarket uses CLOB API (different endpoints)
- **Authentication**: API keys instead of RSA signing
- **Market IDs**: Different ticker format
- **Position Tracking**: CSV instead of Google Sheets (for now)

## Legal Notice

⚠️ **Important**: Using VPN to access geoblocked services may violate Terms of Service. Use at your own risk.
