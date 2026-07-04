"""
manage_timestamps.py — finalize and check OpenTimestamps proofs for
SpeedWatch evidence snapshots.

Stamping (done automatically by analyze.py) is instant, but the underlying
Bitcoin confirmation takes hours. Run this script later to attempt to
finalize proofs, and to check which ones are confirmed.

Usage (inside the container):
    docker compose exec speedwatch python manage_timestamps.py upgrade
    docker compose exec speedwatch python manage_timestamps.py verify
    docker compose exec speedwatch python manage_timestamps.py   # does both
"""

import os
import sys
import glob
import subprocess

OUT_DIR = os.environ.get("OUT_DIR", "/data/reports")
SNAPSHOT_DIR = os.path.join(OUT_DIR, "snapshots")


def find_ots_files():
    return sorted(glob.glob(os.path.join(SNAPSHOT_DIR, "*.ots")))


def upgrade_all(timeout: int = 30):
    ots_files = find_ots_files()
    if not ots_files:
        print("No .ots proofs found yet — run analyze.py first.")
        return
    for f in ots_files:
        print(f"Upgrading {os.path.basename(f)} ...")
        try:
            proc = subprocess.run(
                ["ots", "upgrade", f], capture_output=True, text=True, timeout=timeout
            )
            out = (proc.stdout.strip() or proc.stderr.strip())
            print(f"  {out}" if out else "  (no output)")
        except Exception as e:
            print(f"  failed: {e}")


def verify_all(timeout: int = 30):
    ots_files = find_ots_files()
    if not ots_files:
        print("No .ots proofs found yet — run analyze.py first.")
        return
    for f in ots_files:
        print(f"Verifying {os.path.basename(f)} ...")
        try:
            proc = subprocess.run(
                ["ots", "verify", f], capture_output=True, text=True, timeout=timeout
            )
            out = (proc.stdout.strip() or proc.stderr.strip())
            print(f"  {out}" if out else "  (no output)")
        except Exception as e:
            print(f"  failed: {e}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "both"
    if action in ("upgrade", "u"):
        upgrade_all()
    elif action in ("verify", "v"):
        verify_all()
    else:
        upgrade_all()
        print()
        verify_all()
