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

On Windows, the log check ends with `| findstr PROBLEM` in place of
`| grep PROBLEM`.

## Restore a backup

    docker compose stop app
    docker compose run --rm app manage list       # the backups, newest first
    docker compose run --rm app manage restore /backups/<file>
    docker compose start app

`list` doesn't open the database, so it works however damaged that is; the
names are also those of the files in `BACKUP_DIR`. The restore asks you to
type `restore`, keeps the replaced database as
`/data/wallet.db.before-restore-<time>`, and is refused while the app runs.
Everyone logs in again, and setup codes given out before the backup no longer
work. [docs/HOSTING.md](../docs/HOSTING.md) section 5 says what else it
undoes, and how to restore on a new server (before its first start).

The app refuses to start when its database is missing but `BACKUP_DIR` has
backups of it (`docker compose logs app` says so): restore one as above, or
use an empty backup folder for a new wallet.

## Update

    docker compose exec app manage backup
    git pull
    docker compose pull --ignore-buildable
    docker compose up -d --build

Behind the tunnel, add `--profile tunnel` to the pull and up lines
(`docker compose --profile tunnel pull --ignore-buildable`,
`docker compose --profile tunnel up -d --build`), and every few months set
`CLOUDFLARED_VERSION` in `.env` to the newest release
(<https://github.com/cloudflare/cloudflared/releases>). Not on a server
without the tunnel: there it would start cloudflared with no token.

## Settings (`.env`)

| Setting | |
|---|---|
| `SITE_ADDRESS` | a domain (automatic HTTPS), `localhost` (to try it here) or `http://:80` (behind the tunnel) |
| `PUBLISH_IP` | `0.0.0.0` on a server, `127.0.0.1` with the tunnel |
| `TUNNEL_TOKEN` | the Cloudflare Tunnel token (tunnel only) |
| `CLOUDFLARED_VERSION` | the cloudflared release (tunnel only; empty: the one `compose.yaml` names) |
| `BACKUP_DIR` | the folder that gets the backups |
| `WALLET_UID`, `WALLET_GID` | who owns the files there (on Linux: `id -u`, `id -g`; leave 10001 if that is 0, root) |
| `WALLET_PING_URL` | optional: called after each hourly check and backup, `/fail` added when something went wrong |

The addresses on the internal network are fixed: Caddy `172.29.83.10` (the
only one the app takes the client's address from) and cloudflared
`172.29.83.11` (the only one Caddy takes `Cf-Connecting-IP` from); other
containers get one from `172.29.83.128/25`.
