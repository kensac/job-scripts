import os
import sys
import urllib.request

url = f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/healthz"
try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
