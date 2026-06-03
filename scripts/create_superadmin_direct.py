#!/usr/bin/env python3
"""Direct superadmin creation — skips product key (for initial dev setup).
Run: uv run python scripts/create_superadmin_direct.py
"""
import getpass, sys, json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://helix:helix@localhost:5432/helix")

def green(s):  return f"\033[0;32m{s}\033[0m"
def red(s):    return f"\033[0;31m{s}\033[0m"

try:
    import bcrypt, psycopg2
except ImportError:
    print("Installing dependencies..."); import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "bcrypt", "psycopg2-binary", "-q"])
    import bcrypt, psycopg2

db = DATABASE_URL.replace("+asyncpg","").replace("+aiosqlite","")
parsed = urlparse(db)
conn = psycopg2.connect(host=parsed.hostname or "localhost", port=parsed.port or 5432,
    dbname=parsed.path.lstrip("/"), user=parsed.username, password=parsed.password)
cur = conn.cursor()

cur.execute("SELECT COUNT(*) FROM helix_users WHERE is_superadmin = TRUE")
if cur.fetchone()[0] > 0:
    print(green("✓ Superadmin already exists. Nothing to do.")); conn.close(); sys.exit(0)

print("\n╔══════════════════════════════════╗")
print("║  Create Velaris Superadmin       ║")
print("╚══════════════════════════════════╝\n")

username = input("  Username [superadmin]: ").strip() or "superadmin"
email    = input(f"  Email [{username}@velaris.local]: ").strip() or f"{username}@velaris.local"

while True:
    pwd1 = getpass.getpass("  Password (min 12 chars): ")
    if len(pwd1) < 12: print(red("  Too short.")); continue
    pwd2 = getpass.getpass("  Confirm password: ")
    if pwd1 != pwd2: print(red("  No match.")); continue
    break

pw_hash = bcrypt.hashpw(pwd1.encode(), bcrypt.gensalt(12)).decode()
cur.execute("SELECT COUNT(*) FROM helix_users WHERE username = %s", (username,))
if cur.fetchone()[0] > 0:
    print(red(f"  Username '{username}' already taken.")); conn.close(); sys.exit(1)

cur.execute("""INSERT INTO helix_users
    (username, email, display_name, password_hash, roles, is_superadmin, is_active)
    VALUES (%s,%s,%s,%s,%s,TRUE,TRUE)""",
    (username, email, "Superadmin", pw_hash, '["superadmin","admin"]'))
conn.commit(); conn.close()

# Write setup marker and key cache
(Path(__file__).parent.parent / ".velaris-setup-complete").write_text(datetime.now(timezone.utc).isoformat())
cache = Path(__file__).parent.parent / ".velaris-key"
if not cache.exists():
    cache.write_text(json.dumps({"key_id":"DEV-LOCAL","status":"active",
        "last_verified": datetime.now(timezone.utc).isoformat(), "grace_days": 30}))

print(f"\n{green('✓')} Superadmin '{username}' created.")
print(green("  Run ./start-velaris.sh\n"))
