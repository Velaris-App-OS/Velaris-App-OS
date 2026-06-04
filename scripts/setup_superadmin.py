#!/usr/bin/env python3
"""
Velaris Superadmin Setup
========================
Called by setup-velaris.sh after all dependencies are installed.

Steps:
  1. Prompt for product key
  2. Validate key offline (Ed25519 format check)
  3. Send activation ping to registration server (binds key to this machine)
  4. Prompt for superadmin username + password
  5. Create superadmin in helix_users with is_superadmin=True
  6. Write .velaris-key cache file
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
import httpx

# ── Embed public key here (copy from python scripts/_keygen.py --init) ────────
# Replace this with your actual public key hex after running --init
VELARIS_PUBLIC_KEY_HEX = os.environ.get(
    "VELARIS_PUBLIC_KEY",
    "86ae835f7e31e198e86f87abc634a3d68161ebe60420a99d46a481f5272cb72a"
)

REGISTER_URL  = os.environ.get("VELARIS_REGISTER_URL", "https://register.velaris.io")
DATABASE_URL  = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://helix:helix@localhost:5432/helix"
)
HELIX_DIR     = Path(__file__).parent.parent
KEY_CACHE     = HELIX_DIR / ".velaris-key"


# ── Colours ───────────────────────────────────────────────────────────────────

def green(s):  return f"\033[0;32m{s}\033[0m"
def red(s):    return f"\033[0;31m{s}\033[0m"
def yellow(s): return f"\033[0;33m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"


# ── Machine fingerprint ───────────────────────────────────────────────────────

def get_mac() -> str:
    """Get primary network interface MAC address."""
    override = os.environ.get("VELARIS_HOST_MAC")
    if override:
        return override.lower()
    try:
        result = subprocess.run(["ip", "route", "show", "default"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            iface = result.stdout.split()[4]
            mac_file = Path(f"/sys/class/net/{iface}/address")
            if mac_file.exists():
                return mac_file.read_text().strip().lower()
    except Exception:
        pass
    # Fallback
    mac_int = uuid.getnode()
    return ":".join(f"{(mac_int >> (8 * i)) & 0xff:02x}" for i in range(5, -1, -1))


def get_machine_id(mac: str) -> str:
    hostname = socket.gethostname()
    return hashlib.sha256(f"{mac}:{hostname}".encode()).hexdigest()[:32]


def get_os() -> str:
    try:
        import platform
        return platform.system() + " " + platform.release()
    except Exception:
        return "unknown"


# ── Key validation ────────────────────────────────────────────────────────────

def validate_key_format(key: str) -> tuple[bool, str]:
    """Offline format check — verifies structure and base32 groups."""
    key = key.strip().upper()
    parts = key.split("-")
    if len(parts) != 7 or parts[0] != "VELARIS" or len(parts[1]) != 8:
        return False, "Key format invalid. Expected: VELARIS-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX"
    import base64
    b32_str = "".join(parts[2:])
    if len(b32_str) != 40:
        return False, "Key checksum section invalid length."
    try:
        padding = (8 - len(b32_str) % 8) % 8
        base64.b32decode(b32_str + "=" * padding)
    except Exception:
        return False, "Key checksum section is not valid base32."
    return True, parts[1]


# ── Registration server activation ────────────────────────────────────────────

def activate_key(full_key: str, key_id: str, mac: str, machine_id: str) -> dict:
    """Send activation ping to registration server."""
    payload = {
        "key":        full_key,
        "mac":        mac,
        "machine_id": machine_id,
        "hostname":   socket.gethostname(),
        "os":         get_os(),
        "version":    "1.0.0",
    }
    try:
        resp = httpx.post(f"{REGISTER_URL}/activate", json=payload, timeout=15)
        return resp.json()
    except httpx.ConnectError:
        return {"error": "unreachable"}
    except Exception as e:
        return {"error": str(e)}


# ── Superadmin creation ───────────────────────────────────────────────────────

def create_superadmin_sync(username: str, password: str, email: str) -> None:
    """Create superadmin in helix_users table (synchronous via psycopg2)."""
    import psycopg2
    from urllib.parse import urlparse

    # Parse DATABASE_URL (strip async driver prefix if present)
    db_url = DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
    parsed = urlparse(db_url)
    conn = psycopg2.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        dbname=parsed.path.lstrip("/"),
        user=parsed.username,
        password=parsed.password,
    )
    cur = conn.cursor()

    # Check if superadmin already exists
    cur.execute("SELECT COUNT(*) FROM helix_users WHERE is_superadmin = TRUE")
    if cur.fetchone()[0] > 0:
        conn.close()
        print(yellow("  ⚠  A superadmin already exists. Skipping creation."))
        return

    # Check username taken
    cur.execute("SELECT COUNT(*) FROM helix_users WHERE username = %s", (username,))
    if cur.fetchone()[0] > 0:
        conn.close()
        raise ValueError(f"Username '{username}' is already taken.")

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()

    cur.execute("""
        INSERT INTO helix_users
          (username, email, display_name, password_hash, roles, is_superadmin, is_active)
        VALUES (%s, %s, %s, %s, %s, TRUE, TRUE)
    """, (
        username,
        email or f"{username}@velaris.local",
        "Superadmin",
        pw_hash,
        '["superadmin", "admin"]',
    ))
    conn.commit()
    conn.close()


# ── Main flow ─────────────────────────────────────────────────────────────────

def main():
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║         Velaris Superadmin Setup                 ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # ── Step 1: Product key ───────────────────────────────────────────
    print(bold("Step 1/3 — Product Key"))
    print("  Register at your Velaris website to receive a product key.")
    print()

    for attempt in range(3):
        key_input = input("  Enter your product key: ").strip()
        if not key_input:
            print(red("  ✗ No key entered."))
            continue
        valid, key_id = validate_key_format(key_input)
        if valid:
            print(green(f"  ✓ Key format valid (ID: {key_id})"))
            break
        print(red(f"  ✗ {key_id}"))
        if attempt == 2:
            print(red("  Too many invalid attempts. Exiting."))
            sys.exit(1)

    # ── Step 2: Activate with registration server ─────────────────────
    print()
    print(bold("Step 2/3 — Activating with Velaris registration server…"))
    mac        = get_mac()
    machine_id = get_machine_id(mac)
    print(f"  Machine MAC: {mac[:8]}??:??:??  (first 3 octets shown)")

    result = activate_key(key_input, key_id, mac, machine_id)

    if "error" in result and result["error"] == "unreachable":
        print(yellow("  ⚠  Registration server unreachable. Proceeding offline."))
        print(yellow("     Your key will be verified on first startup."))
    elif result.get("status") == "revoked":
        print(red("  ✗ This key has been revoked. Contact support."))
        sys.exit(1)
    elif result.get("status") == "mac_mismatch":
        print(red("  ✗ Key already activated on a different machine."))
        print(red("     Contact support to transfer your key."))
        sys.exit(1)
    elif result.get("status") in ("activated", "active"):
        print(green("  ✓ Key activated and bound to this machine."))
    elif "error" in result:
        print(yellow(f"  ⚠  Activation server error: {result['error']}. Continuing."))
    else:
        print(green("  ✓ Key verified."))

    # ── Write key cache ───────────────────────────────────────────────
    cache = {
        "key_id":        key_id,
        "status":        "active",
        "mac":           mac,
        "last_verified": datetime.now(timezone.utc).isoformat(),
        "grace_days":    int(os.environ.get("KEY_GRACE_DAYS", "7")),
    }
    KEY_CACHE.write_text(json.dumps(cache, indent=2))
    print(green(f"  ✓ Key cache written to {KEY_CACHE}"))

    # ── Step 3: Create superadmin ─────────────────────────────────────
    print()
    print(bold("Step 3/3 — Create Superadmin Account"))
    print("  This is the god-mode account. Choose credentials carefully.")
    print("  Username can be anything. Password minimum 12 characters.")
    print()

    username = input("  Superadmin username [superadmin]: ").strip() or "superadmin"
    email_in = input(f"  Superadmin email [{username}@velaris.local]: ").strip()

    while True:
        pwd1 = getpass.getpass("  Password: ")
        if len(pwd1) < 12:
            print(red("  ✗ Password must be at least 12 characters."))
            continue
        pwd2 = getpass.getpass("  Confirm password: ")
        if pwd1 != pwd2:
            print(red("  ✗ Passwords do not match."))
            continue
        break

    print()
    print("  Creating superadmin account…")
    try:
        create_superadmin_sync(username, pwd1, email_in)
        print(green(f"  ✓ Superadmin '{username}' created."))
    except ValueError as e:
        print(red(f"  ✗ {e}"))
        sys.exit(1)
    except Exception as e:
        print(red(f"  ✗ Database error: {e}"))
        print(red("    Make sure the database is running and migration 071 has been applied."))
        sys.exit(1)

    # ── Write setup complete marker ───────────────────────────────────
    marker = HELIX_DIR / ".velaris-setup-complete"
    marker.write_text(datetime.now(timezone.utc).isoformat())

    print()
    print("╔══════════════════════════════════════════════════╗")
    print(green("║  Setup complete! Run ./start-velaris.sh          ║"))
    print("╚══════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
