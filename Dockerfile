FROM python:3.11-slim

WORKDIR /app

# Official Ookla Speedtest CLI — the old unmaintained `speedtest-cli` Python
# library was archived in Jan 2026 and now gets blocked (403) by Ookla's
# servers. This binary is actively maintained by Ookla itself.
#
# traceroute + whois are kept at runtime (not purged) — they're used by
# network_check.py to prove which network the container's public IP
# actually belongs to, as independent evidence for a throttling complaint.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg apt-transport-https dirmngr ca-certificates \
        traceroute whois dnsutils && \
    curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash && \
    apt-get install -y --no-install-recommends speedtest && \
    apt-get purge -y gnupg apt-transport-https dirmngr && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# /data is where the SQLite DB and generated reports live — mount a volume here
VOLUME ["/data"]

CMD ["python", "main.py"]
