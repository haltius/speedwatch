"""
verify_integrity.py — walks the results table's hash chain and reports
whether it's intact.

Run inside the container:
    docker compose exec speedwatch python verify_integrity.py

What this proves: no row was silently edited after being written, because
each row's hash incorporates the previous row's hash — changing any
historical value breaks the chain from that point forward.

What this does NOT prove: that the data was genuine to begin with. This
check runs entirely inside your own environment, so it can't independently
verify authenticity the way result_url (hosted on Ookla's servers) or
network_checks (WHOIS/reverse DNS/traceroute, from systems outside this
container) can. Use this as one supporting piece of evidence, not the
whole case.
"""

import os
import sqlite3

import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

import main as sw_main  # reuses _compute_row_hash so the check matches how hashes were made

DB_PATH = os.environ.get("DB_PATH", "/data/speedwatch.db")


def verify() -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT id, timestamp_utc, download_mbps, upload_mbps, ping_ms, jitter_ms,
               server_name, server_id, server_country, client_isp, client_ip,
               result_url, result_id, error, row_hash, prev_hash
        FROM results ORDER BY id ASC
        """
    ).fetchall()
    conn.close()

    if not rows:
        logger.info("No rows to verify yet.")
        return

    expected_prev = "GENESIS"
    breaks = []

    for r in rows:
        (rid, timestamp_utc, download_mbps, upload_mbps, ping_ms, jitter_ms,
         server_name, server_id, server_country, client_isp, client_ip,
         result_url, result_id, error, row_hash, prev_hash) = r

        if row_hash is None:
            # Rows written before the hash chain feature existed.
            logger.info(f"  #{rid}: no hash recorded (predates integrity chain) — skipping")
            expected_prev = "GENESIS"  # chain effectively restarts here
            continue

        if prev_hash != expected_prev:
            breaks.append((rid, "prev_hash mismatch", prev_hash, expected_prev))

        row_data = {
            "timestamp_utc": timestamp_utc, "download_mbps": download_mbps,
            "upload_mbps": upload_mbps, "ping_ms": ping_ms, "jitter_ms": jitter_ms,
            "server_name": server_name, "server_id": server_id,
            "server_country": server_country, "client_isp": client_isp,
            "client_ip": client_ip, "result_url": result_url,
            "result_id": result_id, "error": error,
        }
        recomputed = sw_main._compute_row_hash(prev_hash, row_data)
        if recomputed != row_hash:
            breaks.append((rid, "row_hash mismatch (data changed after being written)",
                           row_hash, recomputed))

        expected_prev = row_hash

    logger.info(f"Checked {len(rows)} rows.")
    if not breaks:
        logger.info("Chain intact — no evidence of post-hoc editing found.")
    else:
        logger.warning(f"FOUND {len(breaks)} BREAK(S):")
        for rid, reason, stored, expected in breaks:
            logger.warning(f"  Row #{rid}: {reason}")
            logger.warning(f"    stored:   {stored}")
            logger.warning(f"    expected: {expected}")


if __name__ == "__main__":
    verify()
