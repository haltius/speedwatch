"""
SpeedWatch analysis — turns logged speed test data into complaint-ready
evidence: a CSV export and charts, including an hour-of-day breakdown that
reveals throttling patterns (e.g. consistent drops every evening).

Run inside the container:
    docker compose exec speedwatch python analyze.py
Output lands in /data (mounted to ./data on the host).
"""

import os
import sqlite3
import datetime
import subprocess

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DB_PATH = os.environ.get("DB_PATH", "/data/speedwatch.db")
OUT_DIR = os.environ.get("OUT_DIR", "/data/reports")

ADVERTISED_DOWN = float(os.environ.get("ADVERTISED_DOWN_MBPS", "0")) or None
ADVERTISED_UP = float(os.environ.get("ADVERTISED_UP_MBPS", "0")) or None

# CRTC/CCTS generally treat sustained speeds below this fraction of the
# advertised rate as evidence worth flagging. 50% is a common informal
# reference point used in ISP speed-test disputes; adjust to taste.
SHORTFALL_THRESHOLD = 0.5


def load_data() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM results WHERE error IS NULL", conn)
    conn.close()
    if df.empty:
        raise SystemExit("No successful speed test results found yet. Let it run longer.")
    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"])
    df["local_ts"] = df["timestamp_utc"].dt.tz_localize("UTC").dt.tz_convert(
        os.environ.get("TZ", "America/Toronto")
    )
    df["hour"] = df["local_ts"].dt.hour
    df["date"] = df["local_ts"].dt.date
    return df


def export_csv(df: pd.DataFrame) -> str:
    path = os.path.join(OUT_DIR, "speedwatch_export.csv")
    df.drop(columns=["local_ts"]).to_csv(path, index=False)
    return path


def export_timestamp_snapshot(df: pd.DataFrame) -> str:
    """
    Write an immutable, uniquely-named CSV snapshot for OpenTimestamps
    stamping. This is deliberately separate from export_csv()'s output:
    that file gets overwritten on every run, which would orphan any
    timestamp proof made against it (the proof matches a specific byte
    sequence — once the file changes, the proof no longer matches).
    Snapshots are never overwritten, so each stamp stays valid forever.
    """
    snap_dir = os.path.join(OUT_DIR, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(snap_dir, f"speedwatch_snapshot_{stamp}.csv")
    df.drop(columns=["local_ts"]).to_csv(path, index=False)
    return path


def stamp_file(path: str, timeout: int = 20) -> dict:
    """
    Submit a file to OpenTimestamps calendar servers, producing a `.ots`
    proof alongside it. This needs outbound internet to the OpenTimestamps
    calendar servers — if that's unreachable (offline, restricted network),
    it fails gracefully rather than blocking the rest of the report.
    """
    try:
        proc = subprocess.run(
            ["ots", "stamp", path],
            capture_output=True, text=True, timeout=timeout,
        )
        ots_path = path + ".ots"
        success = proc.returncode == 0 and os.path.exists(ots_path)
        return {
            "success": success,
            "ots_path": ots_path if success else None,
            "message": proc.stdout.strip() or proc.stderr.strip(),
        }
    except Exception as e:
        return {"success": False, "ots_path": None, "message": str(e)}


def _fit_date_axis(ax, series: pd.Series) -> None:
    """
    Explicitly scale the x-axis to the actual span of the data instead of
    relying on matplotlib's autoscale, which can balloon out to a
    misleadingly wide default range (months) when there are few points or
    a single point. Also picks an adaptive tick formatter so labels make
    sense whether the span is minutes, hours, days, or weeks.
    """
    tmin, tmax = series.min(), series.max()
    span = tmax - tmin

    if span <= pd.Timedelta(0):
        # Single point (or all identical timestamps): pad by a fixed window
        # so the point is visible with context, not floating in a vast range.
        pad = pd.Timedelta(hours=1)
    else:
        # Pad by 5% of the span on each side, minimum a few minutes.
        pad = max(span * 0.05, pd.Timedelta(minutes=5))

    ax.set_xlim(tmin - pad, tmax + pad)

    locator = mdates.AutoDateLocator(minticks=3, maxticks=8)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))


def chart_timeseries(df: pd.DataFrame) -> str:
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df["local_ts"], df["download_mbps"], label="Download (Mbps)",
            linewidth=1, marker="o", markersize=4)
    ax.plot(df["local_ts"], df["upload_mbps"], label="Upload (Mbps)",
            linewidth=1, marker="o", markersize=4)
    if ADVERTISED_DOWN:
        ax.axhline(ADVERTISED_DOWN, color="green", linestyle="--", linewidth=1,
                   label=f"Advertised download ({ADVERTISED_DOWN} Mbps)")
    if ADVERTISED_UP:
        ax.axhline(ADVERTISED_UP, color="orange", linestyle="--", linewidth=1,
                   label=f"Advertised upload ({ADVERTISED_UP} Mbps)")
    ax.set_title("Measured Speed Over Time")
    ax.set_xlabel("Date/time (local)")
    ax.set_ylabel("Mbps")
    _fit_date_axis(ax, df["local_ts"])
    ax.legend()
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "timeseries.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_hourly_pattern(df: pd.DataFrame) -> str:
    hourly = df.groupby("hour")[["download_mbps", "upload_mbps"]].mean()
    fig, ax = plt.subplots(figsize=(10, 5))
    hourly["download_mbps"].plot(kind="bar", ax=ax, color="steelblue", label="Avg download")
    if ADVERTISED_DOWN:
        ax.axhline(ADVERTISED_DOWN, color="green", linestyle="--", linewidth=1,
                   label=f"Advertised download ({ADVERTISED_DOWN} Mbps)")
    ax.set_title("Average Download Speed by Hour of Day (local time)\n"
                  "A consistent dip at the same hours each day is the throttling signature")
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("Avg Mbps")
    ax.legend()
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "hourly_pattern.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_ping_jitter(df: pd.DataFrame) -> str:
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(df["local_ts"], df["ping_ms"], label="Ping (ms)", linewidth=1,
            color="purple", marker="o", markersize=4)
    if df["jitter_ms"].notna().any():
        ax.plot(df["local_ts"], df["jitter_ms"], label="Jitter (ms)", linewidth=1,
                color="brown", marker="o", markersize=4)
    ax.set_title("Ping / Jitter Over Time")
    ax.set_xlabel("Date/time (local)")
    ax.set_ylabel("ms")
    _fit_date_axis(ax, df["local_ts"])
    ax.legend()
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "ping_jitter.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def summary_report(df: pd.DataFrame) -> str:
    lines = []
    lines.append(f"SpeedWatch Summary Report — generated {datetime.datetime.now().isoformat()}")
    lines.append(f"Total successful tests: {len(df)}")
    lines.append(f"Date range: {df['local_ts'].min()} to {df['local_ts'].max()}")
    lines.append("")
    lines.append(f"Average download: {df['download_mbps'].mean():.2f} Mbps "
                  f"(min {df['download_mbps'].min():.2f}, max {df['download_mbps'].max():.2f})")
    lines.append(f"Average upload:   {df['upload_mbps'].mean():.2f} Mbps "
                  f"(min {df['upload_mbps'].min():.2f}, max {df['upload_mbps'].max():.2f})")
    lines.append(f"Average ping:     {df['ping_ms'].mean():.2f} ms")
    lines.append("")

    if ADVERTISED_DOWN:
        threshold = ADVERTISED_DOWN * SHORTFALL_THRESHOLD
        shortfall = df[df["download_mbps"] < threshold]
        pct = 100 * len(shortfall) / len(df)
        lines.append(
            f"Tests below {int(SHORTFALL_THRESHOLD*100)}% of advertised download "
            f"({threshold:.1f} Mbps): {len(shortfall)} of {len(df)} ({pct:.1f}%)"
        )
        if not shortfall.empty:
            worst_hours = shortfall["hour"].value_counts().sort_index()
            lines.append("Shortfall occurrences by hour of day:")
            for h, c in worst_hours.items():
                lines.append(f"  {h:02d}:00 - {c} occurrences")
    else:
        lines.append("Set ADVERTISED_DOWN_MBPS / ADVERTISED_UP_MBPS env vars for shortfall analysis.")

    lines.append("")
    hourly = df.groupby("hour")["download_mbps"].mean().sort_values()
    lines.append("Three worst average hours (local time) for download speed:")
    for h, v in hourly.head(3).items():
        lines.append(f"  {h:02d}:00 - avg {v:.2f} Mbps")
    lines.append("Three best average hours (local time) for download speed:")
    for h, v in hourly.tail(3).items():
        lines.append(f"  {h:02d}:00 - avg {v:.2f} Mbps")

    lines.append("")
    if "result_url" in df.columns:
        with_url = df["result_url"].notna().sum()
        lines.append(
            f"Tests with an independently-verifiable Ookla result URL: "
            f"{with_url} of {len(df)} ({100*with_url/len(df):.1f}%)"
        )
        lines.append(
            "Each URL is hosted on Ookla's own servers — not editable from "
            "this tool — and can be opened directly to confirm the recorded "
            "speed, ISP, and timestamp independently."
        )
    lines.append(
        "Run `python verify_integrity.py` to check the tamper-evident hash "
        "chain across all logged rows (detects any row edited after being "
        "written)."
    )

    text = "\n".join(lines)
    path = os.path.join(OUT_DIR, "summary.txt")
    with open(path, "w") as f:
        f.write(text)
    return path


def export_network_evidence() -> str | None:
    """
    Pull the most recent network_checks row and write it out as a plain-text
    file — this is the WHOIS/reverse-DNS/traceroute evidence proving which
    network the test device's public IP actually belongs to, suitable to
    attach directly to a CCTS/CRTC complaint.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            """
            SELECT nc.timestamp_utc, nc.public_ip, nc.reverse_dns, nc.whois_org,
                   nc.whois_raw, nc.traceroute_raw, nc.linked_result_id,
                   r.timestamp_utc, r.download_mbps, r.upload_mbps
            FROM network_checks nc
            LEFT JOIN results r ON r.id = nc.linked_result_id
            ORDER BY nc.id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    (nc_ts, public_ip, reverse_dns, whois_org, whois_raw, traceroute_raw,
     linked_result_id, result_ts, result_down, result_up) = row

    lines = [
        "SpeedWatch Network Provenance Evidence",
        f"Network check captured (UTC): {nc_ts}",
    ]
    if linked_result_id is not None and result_ts is not None:
        gap = abs((pd.to_datetime(nc_ts) - pd.to_datetime(result_ts)).total_seconds())
        lines.append(
            f"Linked to speed test result #{linked_result_id} at {result_ts} UTC "
            f"(down={result_down} Mbps, up={result_up} Mbps) — {gap:.0f}s apart"
        )
    else:
        lines.append("Not linked to a specific speed test result.")
    lines += [
        f"Public IP under test: {public_ip}",
        "",
        "--- Reverse DNS (PTR record) ---",
        reverse_dns or "(lookup failed or no PTR record)",
        "",
        "--- WHOIS organization (parsed) ---",
        whois_org or "(could not parse an org field)",
        "",
        "--- WHOIS raw output (ARIN/registry record) ---",
        whois_raw or "(whois lookup failed)",
        "",
        "--- Traceroute (first hops are normally inside the ISP's own network) ---",
        traceroute_raw or "(traceroute failed)",
    ]
    text = "\n".join(lines)
    path = os.path.join(OUT_DIR, "network_evidence.txt")
    with open(path, "w") as f:
        f.write(text)
    return path


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    df = load_data()
    csv_path = export_csv(df)
    ts_path = chart_timeseries(df)
    hourly_path = chart_hourly_pattern(df)
    pj_path = chart_ping_jitter(df)
    summary_path = summary_report(df)
    network_path = export_network_evidence()

    logger.info("Generated:")
    for p in [csv_path, ts_path, hourly_path, pj_path, summary_path, network_path]:
        if p:
            logger.info(f"  {p}")
    if network_path is None:
        logger.info("  (no network_checks data yet — let the container run a bit longer)")

    snapshot_path = export_timestamp_snapshot(df)
    logger.info(f"  {snapshot_path} (immutable snapshot for timestamping)")
    stamp_result = stamp_file(snapshot_path)
    if stamp_result["success"]:
        logger.info(f"  {stamp_result['ots_path']} (OpenTimestamps proof — pending Bitcoin confirmation)")
        logger.info(
            "    Run `python manage_timestamps.py upgrade` in a few hours "
            "to attempt to finalize this proof, then `verify` to confirm."
        )
    else:
        logger.warning(f"  OpenTimestamps stamping failed: {stamp_result['message']}")
        logger.warning("  (needs outbound internet to OpenTimestamps calendar servers — safe to ignore if offline)")


if __name__ == "__main__":
    main()
