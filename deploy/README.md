# Running the wallet with Docker

The commands only. [docs/HOSTING.md](../docs/HOSTING.md) explains where to
host it, each setting, the backups and what to do after a disaster.

What runs: `app` (the wallet, `python -m app.serve`: the ledger check and a
backup every hour), `caddy` (HTTPS in front of it) and, with the `tunnel`
profile, `cloudflared` (a Cloudflare Tunnel for a computer without a public
address). The database lives in the Docker volume `wallet-data`; the backups
go to the folder `BACKUP_DIR` (default `./backups`, next to this file).

Run every command from this folder.

## Start

    cp .env.example .env          # then fill it in
    docker compose up -d --build                  # a server with a domain
    docker compose --profile tunnel up -d --build # behind the tunnel
    docker compose ps                             # app should be "healthy"
    docker compose exec app manage first-admin --name "Your name" --phone "091 000 0000"

Check it answers: `https://<SITE_ADDRESS>/api/ping` shows `{"ok": true}`.

## Every day use

    docker compose exec app manage backup         # a backup now
    docker compose exec app manage list           # the backups, newest first
    docker compose exec app manage check          # the database and the ledger
    docker compose exec app manage check-backup /backups/<file>
    docker compose logs --since 24h app | grep PROBLEM
    docker compose stop                           # stop (start: docker compose start)

## Restore a backup

    docker compose stop app
    docker compose run --rm app manage restore /backups/<file>
    docker compose start app

The restore asks you to type `restore`, keeps the replaced database as
`/data/wallet.db.before-restore-<time>`, and is refused while the app runs.

## Update

    docker compose exec app manage backup
    git pull
    docker compose pull --ignore-buildable
    docker compose up -d --build

## Settings (`.env`)

| Setting | |
|---|---|
| `SITE_ADDRESS` | a domain (automatic HTTPS), `localhost` (to try it here) or `http://:80` (behind the tunnel) |
| `PUBLISH_IP` | `0.0.0.0` on a server, `127.0.0.1` with the tunnel |
| `TUNNEL_TOKEN` | the Cloudflare Tunnel token (tunnel only) |
| `BACKUP_DIR` | the folder that gets the backups |
| `WALLET_UID`, `WALLET_GID` | who owns the files there (on Linux: `id -u`, `id -g`) |
| `WALLET_PING_URL` | optional: called after each hourly check and backup, `/fail` added when something went wrong |

The addresses on the internal network are fixed: Caddy `172.29.83.10` (the
only one the app takes the client's address from) and cloudflared
`172.29.83.11` (the only one Caddy takes `Cf-Connecting-IP` from); other
containers get one from `172.29.83.128/25`.
