# SpeedWatch

A small Dockerized tool that runs real internet speed tests on a schedule,
logs the results to SQLite, and generates charts/reports suitable as
evidence for an ISP complaint in Canada (CCTS or CRTC — see note below).

## What it does

- Runs a full Ookla-protocol speed test (download, upload, ping) every
  `INTERVAL_MINUTES` (default: 30 min) via the `speedtest-cli` library
- Logs every result — including failures — with a UTC timestamp to
  `./data/speedwatch.db` (SQLite)
- `analyze.py` turns the logged data into:
  - `speedwatch_export.csv` — full raw export
  - `timeseries.png` — speed over time, with your advertised plan speed as
    a reference line
  - `hourly_pattern.png` — average download speed by hour of day. **This is
    the chart that matters most for a throttling complaint** — a
    repeatable dip at the same hours every day is the throttling
    signature CRTC/CCTS actually look for. Random noise with no time
    pattern points more toward general congestion or a line issue instead.
  - `ping_jitter.png` — latency trend
  - `network_evidence.txt` — WHOIS, reverse DNS, and traceroute output
    proving which network the test device's public IP belongs to (see
    "Proving which network you're actually on" below)
  - `summary.txt` — plain-text stats, including % of tests that fell below
    a configurable fraction of your advertised speed, broken down by hour

> **Note:** this uses Ookla's official `speedtest` CLI binary (installed in
> the Dockerfile), not the old Python `speedtest-cli` library — that
> project was archived by its maintainer in January 2026 and now gets
> blocked (HTTP 403) by Ookla's servers.

## Proving the data itself isn't fabricated

A local SQLite file is trivially editable — "the database says X" isn't
proof by itself. Here's what SpeedWatch adds, and being honest about what
each one actually establishes:

1. **Ookla's own result URL (strongest).** Every test uploads to Ookla's
   servers and gets a unique `result_url` you didn't create and can't
   edit. Anyone reviewing your complaint can open it directly and see
   Ookla's own hosted, timestamped record of that test. `summary.txt`
   reports what fraction of your tests have one.
2. **Tamper-evident hash chain (medium — local only).** Each row in
   `results` stores a hash of its own data plus the previous row's hash.
   Run `docker compose exec speedwatch python verify_integrity.py` and it
   will walk the whole chain and report any row whose data no longer
   matches its hash, or any broken link between rows — both indicate a
   row was edited after being written.
   **Honest limitation:** this only proves nothing was silently edited
   *after the fact*. It does not independently prove the data was genuine
   to begin with, because the entire chain is generated and checked
   inside your own environment — someone with DB access and the (open)
   hashing algorithm could still tamper with the data and regenerate a
   self-consistent chain from scratch. For that, you need #1 and #3.
3. **OpenTimestamps (strongest, now automated).** A free, widely-used
   method that timestamps a cryptographic hash of a file against the
   Bitcoin blockchain, so anyone can independently verify the file existed
   with that exact content at that exact time — without trusting you,
   this tool, or Ookla.

   Every `analyze.py` run now writes an **immutable snapshot** to
   `./data/reports/snapshots/speedwatch_snapshot_<timestamp>.csv` (never
   overwritten — each one is a permanent point-in-time copy) and submits
   it to OpenTimestamps calendar servers, producing a `.ots` proof file
   alongside it.

   Stamping is instant, but full Bitcoin confirmation takes hours. Run
   this later (e.g. the next day) to finalize and check proofs:

   ```bash
   docker compose exec speedwatch python manage_timestamps.py upgrade
   docker compose exec speedwatch python manage_timestamps.py verify
   ```

   Keep the `snapshots/` folder intact — a `.ots` proof only verifies
   against the exact file it was made for, so deleting or editing a
   snapshot invalidates its proof.

## Proving which network you're actually on

A self-declared "I was on Rogers Wi-Fi" label isn't evidence — it's an
assertion. What actually holds up is independently verifiable data that
Claude/you didn't write, sourced from systems outside your control:

- **WHOIS** on your public IP — the internet registry's (ARIN's)
  authoritative record of which organization that IP block is allocated
  to.
- **Reverse DNS (PTR record)** — the ISP's own DNS answering "whose IP is
  this." Rogers residential IPs commonly resolve to a hostname containing
  `rogers.com`.
- **Traceroute** — the actual path your traffic takes off your network.
  The first few hops are normally inside the ISP's own infrastructure and
  are frequently named accordingly.

SpeedWatch runs all three automatically every `NETWORK_CHECK_INTERVAL_HOURS`
(default: 12) and stores them in the `network_checks` table alongside your
speed data — same database, same timestamps, so they're tied to the exact
test run they correspond to.

Running `analyze.py` produces `network_evidence.txt` with the most recent
capture: raw WHOIS record, reverse DNS, and traceroute output — ready to
attach directly to a CCTS/CRTC complaint as the evidence that the device
running these tests is actually on Rogers' network.

## Setup

> **If you're upgrading from an earlier version:** the `results` table
> gained new columns (`result_url`, `result_id`, `row_hash`, `prev_hash`).
> SQLite's `CREATE TABLE IF NOT EXISTS` won't retroactively add columns to
> a table that already exists. Either delete `./data/speedwatch.db` and
> start fresh (loses existing history), or run a manual `ALTER TABLE`
> migration before restarting — ask if you'd like that script.

1. Edit `docker-compose.yml` and set `ADVERTISED_DOWN_MBPS` /
   `ADVERTISED_UP_MBPS` to what your plan promises, and `TZ` to your
   timezone (defaults are already set for Ontario).
2. Build and start it:

   ```bash
   docker compose up -d --build
   ```

3. Let it run. **Two to four weeks of continuous data is what actually
   makes a complaint credible** — a day or two of samples is too easily
   dismissed as normal variance. Check it's alive with:

   ```bash
   docker compose logs -f speedwatch
   ```

4. Generate a report any time:

   ```bash
   docker compose exec speedwatch python analyze.py
   ```

   Output appears in `./data/reports/` on the host (visible outside the
   container since `./data` is a shared volume).

## Notes on evidence quality

- Test frequency matters more than test count — 30 min is a good default;
  going much below that can eat into a data cap over weeks. Going much
  above it risks missing short throttling windows.
- If your ISP throttles specific services (e.g. streaming or P2P) rather
  than raw bandwidth, a generic speed test won't detect it — that needs
  per-service testing, which is a different (and messier) setup.
- Keep the machine running this on a wired connection if possible; Wi-Fi
  variance can muddy the signal you're trying to prove.
- Hang on to your ISP plan documentation (contracted speed, contract PDF,
  any promo terms) — the charts are only half the case; the advertised
  number they're compared against is the other half.

## Filing the complaint (Canada)

Two different paths depending on what you're seeing, both require
contacting your ISP first:

- **Getting less speed than you're paying for, no particular time
  pattern** → this is a quality-of-service/billing issue. The CRTC
  doesn't handle this directly — contact your ISP, then escalate to the
  **CCTS** (Commission for Complaints for Telecom-television Services) at
  [ccts-cprst.ca](https://ccts-cprst.ca/complaints/complaint-form) if
  unresolved. Free for consumers.
- **Speed drops at consistent times of day (the pattern
  `hourly_pattern.png` is built to reveal)** → this looks like actual
  traffic management/throttling, which the **CRTC** handles directly
  under its Internet Traffic Management Practices framework. Contact your
  ISP first; if unresolved, submit to CRTC
  ([crtc.gc.ca](https://crtc.gc.ca/eng/internet/traf.htm)).

Either way, attach the CSV, the charts, and your plan's advertised speed.
