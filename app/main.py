"""
SpeedWatch — periodic internet speed logger for ISP throttling evidence.

Runs a real speed test (Ookla protocol via the `speedtest` library) on a
schedule, and logs timestamp, download/upload throughput, ping, and jitter
to a local SQLite database. Designed to run unattended in a Docker
container for weeks at a time.
"""

import os
import sys
import json
import time
import hashlib
import sqlite3
import datetime
import subprocess

import network_check
import threading

import logging

logger = logging.getLogger('speedwatch')
logger.setLevel(logging.DEBUG)

# create console handler with a higher log level
ch = logging.StreamHandler()
ch.setLevel(logging.ERROR)

# create file handler which logs even debug messages
fh = logging.FileHandler('/data/speedwatch.log')
fh.setLevel(logging.DEBUG)

# create formatter and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
fh.setFormatter(formatter)
# add the handlers to logger
logger.addHandler(ch)
logger.addHandler(fh)


DB_PATH = os.environ.get("DB_PATH", "/data/speedwatch.db")
INTERVAL_MINUTES = float(os.environ.get("INTERVAL_MINUTES", "30"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))
SPEEDTEST_TIMEOUT_SECONDS = int(os.environ.get("SPEEDTEST_TIMEOUT_SECONDS", "120"))

# How often to re-run the WHOIS/reverse-DNS/traceroute network check. This
# doesn't need to run every speed test — the underlying facts (who owns
# your IP block) rarely change more than once a day, and traceroute/whois
# are a bit heavier than a plain speed test.
NETWORK_CHECK_INTERVAL_HOURS = float(os.environ.get("NETWORK_CHECK_INTERVAL_HOURS", "12"))

CONNECTION_LOG_PATH = os.environ.get("CONNECTION_LOG_PATH", "/data/connection_log.csv")
CONNECTION_MAX_AGE_MINUTES = float(os.environ.get("CONNECTION_MAX_AGE_MINUTES", "10"))

# --- Watchdog: detects a hung main loop and forces a restart ---
# Docker's `restart: unless-stopped` only recovers from a process that
# actually crashes — it does nothing for one that's alive but stuck (e.g.
# blocked on a wedged bind mount). This makes a hang visible in the logs
# and forces a hard exit so the restart policy has something to react to.
_last_heartbeat = time.time()
_heartbeat_lock = threading.Lock()

def _touch_heartbeat():
    global _last_heartbeat
    with _heartbeat_lock:
        _last_heartbeat = time.time()

WATCHDOG_CHECK_SECONDS = int(os.environ.get("WATCHDOG_CHECK_SECONDS", "60"))
WATCHDOG_STUCK_THRESHOLD_SECONDS = max(
    INTERVAL_MINUTES * 60 * 3,        # 3 missed cycles
    SPEEDTEST_TIMEOUT_SECONDS * 2,    # or 2x the speedtest's own timeout, whichever's bigger
)

def _watchdog_loop():
    while True:
        time.sleep(WATCHDOG_CHECK_SECONDS)
        with _heartbeat_lock:
            age = time.time() - _last_heartbeat
        if age > WATCHDOG_STUCK_THRESHOLD_SECONDS:
            logger.critical(
                f"[watchdog] no heartbeat in {age:.0f}s "
                f"(threshold {WATCHDOG_STUCK_THRESHOLD_SECONDS:.0f}s) — "
                f"main loop appears stuck. Forcing exit so Docker can restart it."
            )
            os._exit(1)  # hard kill — bypasses cleanup on purpose, in case
                         # normal shutdown would also hang on the same stuck I/O

def get_latest_connection_info() -> dict:
    """
    Read the most recent row written by the host-side detect_connection.sh
    (via launchd, independent of this container) and attach it if it's
    recent enough to actually describe this moment.
    """
    if not os.path.exists(CONNECTION_LOG_PATH):
        return {"connection_type": "unknown", "connection_confidence": "no_data"}
    try:
        with open(CONNECTION_LOG_PATH, "r") as f:
            lines = f.read().strip().split("\n")
        if len(lines) < 2:
            return {"connection_type": "unknown", "connection_confidence": "no_data"}

        import csv
        header = lines[0].split(",")
        fields = next(csv.reader([lines[-1]]))
        row = dict(zip(header, fields))

        ts = datetime.datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
        age = datetime.datetime.now(datetime.timezone.utc) - ts
        if age > datetime.timedelta(minutes=CONNECTION_MAX_AGE_MINUTES):
            return {"connection_type": "unknown", "connection_confidence": "stale"}

        return {
            "connection_type": row.get("connection_type", "unknown"),
            "connection_confidence": row.get("confidence", "no_data"),
        }
    except Exception:
        return {"connection_type": "unknown", "connection_confidence": "no_data"}

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            download_mbps REAL,
            upload_mbps REAL,
            ping_ms REAL,
            jitter_ms REAL,
            server_name TEXT,
            server_id TEXT,
            server_country TEXT,
            client_isp TEXT,
            client_ip TEXT,
            result_url TEXT,
            result_id TEXT,
            error TEXT,
            row_hash TEXT,
            prev_hash TEXT,
            connection_type TEXT,
            connection_confidence TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS network_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            linked_result_id INTEGER,
            public_ip TEXT,
            reverse_dns TEXT,
            whois_org TEXT,
            whois_raw TEXT,
            traceroute_raw TEXT,
            FOREIGN KEY (linked_result_id) REFERENCES results(id)
        )
        """
    )
    conn.commit()


def run_speedtest() -> dict:
    """
    Run one speed test using Ookla's official CLI binary and return a flat
    dict of results. Raises on failure (non-zero exit, timeout, bad JSON).
    """
    proc = subprocess.run(
        ["speedtest", "--accept-license", "--accept-gdpr", "-f", "json"],
        capture_output=True,
        text=True,
        timeout=SPEEDTEST_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"speedtest exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )

    res = json.loads(proc.stdout)

    download_bw = res.get("download", {}).get("bandwidth")  # bytes/sec
    upload_bw = res.get("upload", {}).get("bandwidth")      # bytes/sec

    return {
        "timestamp_utc": datetime.datetime.utcnow().isoformat(),
        "download_mbps": round(download_bw * 8 / 1_000_000, 2) if download_bw else None,
        "upload_mbps": round(upload_bw * 8 / 1_000_000, 2) if upload_bw else None,
        "ping_ms": round(res.get("ping", {}).get("latency"), 2)
        if res.get("ping", {}).get("latency") is not None else None,
        "jitter_ms": round(res.get("ping", {}).get("jitter"), 2)
        if res.get("ping", {}).get("jitter") is not None else None,
        "server_name": res.get("server", {}).get("name"),
        "server_id": res.get("server", {}).get("id"),
        "server_country": res.get("server", {}).get("location") or res.get("server", {}).get("country"),
        "client_isp": res.get("isp"),
        "client_ip": res.get("interface", {}).get("externalIp"),
        # Ookla hosts this result itself, at a URL you didn't create and
        # can't edit — the single strongest piece of "this isn't fabricated"
        # evidence this tool can produce, since it's independently viewable
        # on Ookla's own servers.
        "result_url": res.get("result", {}).get("url"),
        "result_id": res.get("result", {}).get("id"),
        "error": None,
    }


def _get_last_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT row_hash FROM results ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row and row[0] else "GENESIS"


_NUMERIC_FIELDS = {"download_mbps", "upload_mbps", "ping_ms", "jitter_ms"}


def _compute_row_hash(prev_hash: str, row: dict) -> str:
    """
    Hash of (previous row's hash + this row's data). Chaining this way means
    editing any historical row breaks the chain from that point forward —
    it's detectable, even though it's still generated locally.

    Important honesty note: this proves the log wasn't silently edited
    *after* being written. It does NOT independently prove the data was
    genuine to begin with — for that, see result_url (Ookla-hosted) and
    network_checks (WHOIS/traceroute), which come from systems outside
    this container's control.
    """
    normalized = {}
    for k in sorted(row.keys()):
        v = row.get(k)
        if k in _NUMERIC_FIELDS and v is not None:
            v = float(v)  # SQLite round-trips REAL columns as float even if
                           # the original value was a Python int — normalize
                           # so verification doesn't produce false mismatches.
        normalized[k] = v
    payload = json.dumps({"prev_hash": prev_hash, **normalized}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log_result(conn: sqlite3.Connection, row: dict) -> int:
    prev_hash = _get_last_hash(conn)
    row_hash = _compute_row_hash(prev_hash, row)

    cur = conn.execute(
        """
        INSERT INTO results
            (timestamp_utc, download_mbps, upload_mbps, ping_ms, jitter_ms,
             server_name, server_id, server_country, client_isp, client_ip,
             result_url, result_id, error, row_hash, prev_hash, connection_type,
             connection_confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["timestamp_utc"], row["download_mbps"], row["upload_mbps"],
            row["ping_ms"], row["jitter_ms"], row["server_name"],
            row["server_id"], row["server_country"], row["client_isp"],
            row["client_ip"], row.get("result_url"), row.get("result_id"),
            row["error"], row_hash, prev_hash, row.get("connection_type"),
            row.get("connection_confidence"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def log_network_check(conn: sqlite3.Connection, evidence: dict, linked_result_id: int | None) -> None:
    conn.execute(
        """
        INSERT INTO network_checks
            (timestamp_utc, linked_result_id, public_ip, reverse_dns, whois_org, whois_raw, traceroute_raw)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.datetime.utcnow().isoformat(), linked_result_id,
            evidence.get("public_ip"), evidence.get("reverse_dns"),
            evidence.get("whois_org"), evidence.get("whois_raw"),
            evidence.get("traceroute_raw"),
        ),
    )
    conn.commit()


def log_error(conn: sqlite3.Connection, message: str) -> None:
    prev_hash = _get_last_hash(conn)
    err_row = {"timestamp_utc": datetime.datetime.utcnow().isoformat(), "error": message}
    row_hash = _compute_row_hash(prev_hash, err_row)
    conn.execute(
        """
        INSERT INTO results (timestamp_utc, error, row_hash, prev_hash)
        VALUES (?, ?, ?, ?)
        """,
        (err_row["timestamp_utc"], message, row_hash, prev_hash),
    )
    conn.commit()


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    logger.info(f"[speedwatch] logging to {DB_PATH}, interval={INTERVAL_MINUTES} min")

    threading.Thread(target=_watchdog_loop, daemon=True).start()

    last_network_check = None

    while True:
        _touch_heartbeat()
        attempt = 0
        row = None
        result_id = None
        while attempt <= MAX_RETRIES:
            try:
                row = run_speedtest()
                row.update(get_latest_connection_info())
                result_id = log_result(conn, row)
                logger.info(
                    f"[speedwatch] {row['timestamp_utc']} "
                    f"down={row['download_mbps']}Mbps up={row['upload_mbps']}Mbps "
                    f"ping={row['ping_ms']}ms",
                )
                break
            except Exception as e:
                attempt += 1
                err = f"{type(e).__name__}: {e}"
                logger.error(f"[speedwatch] ERROR (attempt {attempt}): {err}")
                logger.exception("Full traceback:")   # <-- replaces traceback.print_exc()
                if attempt > MAX_RETRIES:
                    log_error(conn, err)
                else:
                    time.sleep(15)

        now = datetime.datetime.utcnow()
        due = (
            last_network_check is None
            or (now - last_network_check) >= datetime.timedelta(hours=NETWORK_CHECK_INTERVAL_HOURS)
        )
        if due:
            public_ip = row.get("client_ip") if row else None
            try:
                logger.info(f"[speedwatch] running network provenance check (ip={public_ip})...")
                evidence = network_check.gather_network_evidence(public_ip)
                log_network_check(conn, evidence, linked_result_id=result_id)
                logger.info(
                    f"[speedwatch] network check: rdns={evidence.get('reverse_dns')} "
                    f"whois_org={evidence.get('whois_org')} linked_result_id={result_id}",
                )
            except Exception as e:
                logger.error(f"[speedwatch] network check failed: {e}")
            last_network_check = now

        time.sleep(max(INTERVAL_MINUTES, 1) * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
