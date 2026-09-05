import os
import sys
from pathlib import Path

os.environ["BOT_TOKEN"] = "test:test_token_123"
os.environ.setdefault("INSECURE_SSL", "0")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("BOT_PASSWORD", "testpw")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))