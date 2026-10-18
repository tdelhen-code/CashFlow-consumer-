# The LYD wallet server (python -m app.serve). deploy/README.md says how to run
# it with Caddy in front. The 3.13 tag on purpose: a rebuild picks up Python
# and Debian security fixes.
FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WALLET_DB=/data/wallet.db \
    WALLET_BACKUP_DIR=/backups

WORKDIR /srv/wallet
COPY requirements.txt .
RUN pip install --require-hashes --no-deps -r requirements.txt
COPY app/ app/
COPY deploy/entrypoint.sh /usr/local/bin/wallet-entrypoint
COPY deploy/manage /usr/local/bin/manage
RUN chmod 755 /usr/local/bin/wallet-entrypoint /usr/local/bin/manage \
    && mkdir -p /data /backups

VOLUME ["/data", "/backups"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/ping', timeout=4)"]
# Starts as root only to hand /data and /backups to the wallet user, then
# runs the command as that user (see deploy/entrypoint.sh).
ENTRYPOINT ["wallet-entrypoint"]
CMD ["python", "-m", "app.serve", "--host", "0.0.0.0", "--port", "8000", "--proxy", "127.0.0.1"]
