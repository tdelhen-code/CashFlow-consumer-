#!/bin/sh
# Runs every command in the container as the wallet user, never as root.
# A folder mounted from the host belongs to whoever made it, so while still
# root it hands /data and /backups to WALLET_UID:WALLET_GID (10001 unless set;
# on Linux set them to your own ids so you can read the backups without
# sudo; 0, root, is taken as 10001), then drops root. Files are made readable
# by that user only: they hold names, phone numbers and password hashes.
set -eu
uid="${WALLET_UID:-10001}"
gid="${WALLET_GID:-10001}"
umask 077
if [ "$(id -u)" = 0 ]; then
    # 0 is what `id -u` prints for root. Said, not refused: with restart:
    # unless-stopped a refusal would only start the container over and over.
    if [ "$uid" = 0 ] || [ "$gid" = 0 ]; then
        if [ "$uid" = 0 ]; then uid=10001; fi
        if [ "$gid" = 0 ]; then gid=10001; fi
        echo "WALLET_UID or WALLET_GID is 0 (root): running as $uid:$gid instead" >&2
    fi
    for dir in /data /backups; do
        if [ -d "$dir" ]; then
            # Some host folders (Docker Desktop on Windows) can't change owner;
            # they are writable by everyone there anyway.
            chown -R "$uid:$gid" "$dir" 2>/dev/null || true
            chmod 700 "$dir" 2>/dev/null || true
        fi
    done
    exec setpriv --reuid="$uid" --regid="$gid" --clear-groups -- "$@"
fi
exec "$@"
