#!/usr/bin/env bash
# Start the Telegram OpenCode Bot

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load BOT_TOKEN from .env if present (not hardcoded in code)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

# Fix SSL for Termux
if [ -f "$PREFIX/lib/python3.14/site-packages/certifi/cacert.pem" ]; then
    export SSL_CERT_FILE="$PREFIX/lib/python3.14/site-packages/certifi/cacert.pem"
    export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
fi

# Check network first
python3 check_network.py
if [ $? -ne 0 ]; then
    echo ""
    echo "⚠️  Your network blocks Telegram. Switch to mobile data or VPN first."
    echo "   Then run this again."
    exit 1
fi

pkill -f "python.*bot.py" 2>/dev/null || true
sleep 1

echo "🤖 Starting Telegram OpenCode Bot..."
echo "   Press Ctrl+C to stop"
echo ""
python3 bot.py