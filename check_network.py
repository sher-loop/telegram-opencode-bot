#!/usr/bin/env python3
"""Check if Telegram API is reachable before starting the bot."""

import os
import sys
import urllib.request
import ssl
from pathlib import Path


def _load_dotenv():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

# Token is required via the BOT_TOKEN environment variable (never hardcoded).
TOKEN = os.environ.get("BOT_TOKEN", "").strip()

URLS = [
    ("https://api.telegram.org", "default"),
    ("https://core.telegram.org", "core"),
]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def check(url, label):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.status == 200 or True
    except urllib.error.HTTPError as e:
        if e.code == 404:  # API without token route
            return True
        return False
    except Exception as e:
        print(f"  {label}: BLOCKED ({type(e).__name__}: {str(e)[:60]})")
        return False


def main():
    if not TOKEN:
        print("❌ BOT_TOKEN is not set!\n")
        print("   Set it before running:")
        print("       export BOT_TOKEN='123456:ABC...'")
        print("   or add it to a `.env` file next to this script.\n")
        return 1
    print("🔍 Checking Telegram API connectivity...")
    ok = check(URLS[0][0], URLS[0][1])
    api_ok = asyncio_check()
    if api_ok:
        print("✅ Telegram API is reachable. Bot can run.")
        return 0
    else:
        print("❌ Telegram API is BLOCKED by your network (firewall/VPN required).")
        print("   Try: switch to mobile data, enable a VPN, or use another WiFi.")
        return 1


def asyncio_check():
    try:
        import asyncio
        import httpx

        async def _t():
            async with httpx.AsyncClient(verify=False) as c:
                try:
                    r = await c.get(f"{URLS[0][0]}/bot{TOKEN}/getMe", timeout=10)
                    data = r.json()
                    return bool(data.get("ok")) or "result" in data
                except Exception as e:
                    print(f"  api.telegram.org: BLOCKED ({type(e).__name__}: {str(e)[:60]})")
                    return False

        return asyncio.run(_t())
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())