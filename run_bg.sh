#!/usr/bin/env bash
# Background bot management
# Usage: ./run_bg.sh [start|stop|status|log]

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

PIDFILE="$SCRIPT_DIR/bot.pid"
LOGFILE="$SCRIPT_DIR/bot.log"

case "${1:-start}" in
    start)
        # Check network first
        python3 check_network.py 2>&1 | tail -2
        if ! python3 check_network.py >/dev/null 2>&1; then
            echo "⚠️  Network blocks Telegram. Use mobile data or VPN first."
            exit 1
        fi
        # Never start a second instance while one is alive
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "Bot already running (PID: $(cat "$PIDFILE"))"
            echo "Only ONE bot instance may run. Kill all instances first: bash $0 stop"
            exit 1
        fi
        echo "🤖 Starting bot..."
        setsid python3 bot.py </dev/null > "$LOGFILE" 2>&1 &
        echo $! > "$PIDFILE"
        sleep 2
        if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "❌ Bot failed to start (another instance may be running)."
            echo "   Check: $LOGFILE"
            rm -f "$PIDFILE" 2>/dev/null
            exit 1
        fi
        echo "Bot started (PID: $(cat "$PIDFILE"))"
        echo "Logs: tail -f $LOGFILE"
        ;;
    stop)
        # Kill ALL bot instances — no stray process may survive a restart
        pkill -f "python.*bot.py" 2>/dev/null || true
        sleep 1
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            kill -9 "$(cat "$PIDFILE")" 2>/dev/null
        fi
        rm -f "$PIDFILE" 2>/dev/null
        echo "Bot stopped (all instances killed)"
        ;;
    status)
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "Running (PID: $(cat "$PIDFILE"))"
        else
            echo "NOT running"
            rm -f "$PIDFILE" 2>/dev/null
        fi
        ;;
    log)
        tail -f "$LOGFILE"
        ;;
    *)
        echo "Usage: $0 {start|stop|status|log}"
        exit 1
        ;;
esac