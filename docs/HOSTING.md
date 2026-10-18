# Hosting the LYD wallet

This guide is for the owner. It covers where to run the wallet, how to set it
up, how the backups work, and what to do when something goes wrong. The
commands alone are in [deploy/README.md](../deploy/README.md).

The wallet runs the same way on any computer with Docker:

- **the app**, which checks the books every hour and makes a backup every hour;
- **Caddy** in front of it, which gives the HTTPS address workers open;
- **Cloudflare Tunnel**, only when the wallet runs on a computer at home or in
  the office.

## 1. Where to host it

| | Your own computer | A rented server |
|---|---|---|
| Cost | Free, plus electricity and a UPS (a battery that keeps it on in a power cut) | Roughly 5 to 10 US dollars a month, plus a domain at roughly 10 to 15 a year. These are approximate: check current prices |
| A power or internet cut | Workers can't reach their money until it comes back | Data centres have backup power and several internet lines |
| Speed for workers | As fast as your internet upload | Fast. A data centre in Europe is close to Libya |
| Who can touch the data | Anyone who can get to the computer | The provider's staff, under contract. Nobody can carry it away |
| If the computer is lost, stolen or breaks | The wallet stops until you restore a backup on another computer | The provider replaces broken hardware. You still need your backups if the server itself is deleted |
| Internet address | A home line has no fixed public address, so it needs a Cloudflare Tunnel (a free account) | Has a fixed address, so you point your domain at it |
| Your work each month | Keep it on, awake and updated; check the backups | Update the app; check the backups |

**Recommendation:** for a money app that many workers depend on, rent a small
Linux server in a European data centre (Germany, France or Italy, for
example), and keep encrypted backups somewhere else (section 4). It stays up
when your power or internet doesn't, nobody can walk off with it, and it is
not slower for workers.

Your own computer is reasonable for trying the wallet out, or for a few
workers while you decide.

Check whether Libyan rules (for example the Central Bank of Libya's) apply to
holding workers' money and personal data.

## 2. On a rented server

1. **Rent a server.** A small Linux server is enough: Ubuntu 24.04 LTS, 1 to 2
   GB of memory, 20 GB of disk or more, in a European data centre. In the
   provider's firewall, allow only SSH (22), HTTP (80) and HTTPS (443).
2. **Get a domain**, or use a subdomain of one you have, such as
   `wallet.example.com`. At its DNS, add an **A record** pointing at the
   server's IPv4 address. Don't add an AAAA (IPv6) record. Docker would pass
   IPv6 visitors on as one address, and the login limits would then count all
   of them as one person.
3. **Install Docker Engine** with the steps at
   <https://docs.docker.com/engine/install/ubuntu/>.
4. **Copy the project to the server**, for example to `/srv/lyd-wallet`
   (with `git clone`, or by copying the folder).
5. **Fill in the settings:**

       cd /srv/lyd-wallet/deploy
       cp .env.example .env
       nano .env

   Set `SITE_ADDRESS=wallet.example.com`. Set `WALLET_UID` and `WALLET_GID`
   to the numbers that `id -u` and `id -g` print, so the backup files belong
   to you.
6. **Start it:**

       docker compose up -d --build
       docker compose ps

   The `app` line should say `healthy` within a minute. Caddy gets the HTTPS
   certificate by itself within a minute or so of starting (the domain must
   already point at the server), and renews it.
7. **Create the first admin.** It asks for the password twice: at least 10
   characters, and not a common one.

       docker compose exec app manage first-admin --name "Your name" --phone "091 000 0000"

8. **Check it.** Open `https://wallet.example.com/api/ping`; it should show
   `{"ok": true}`.
9. **Set up the off-site backups and the alert** (section 4). Don't skip this.

## 3. On your own computer

1. **Install Docker Desktop** (on Windows, with the WSL 2 option). In its
   settings, have it start when you sign in.
2. **Keep the computer on.** In the power settings, set it never to sleep.
   Use a UPS.
3. **Set up a Cloudflare Tunnel.** Make a free Cloudflare account and add
   your domain to it (Cloudflare then runs the domain's DNS). In the
   Cloudflare dashboard:
   - create a tunnel of the *cloudflared* kind, and copy its **token**;
   - add a public hostname such as `wallet.example.com`, with the service
     `http://caddy:80`.

   Cloudflare gives the HTTPS. The tunnel works because Cloudflare decrypts
   the traffic on its way through, so this means trusting Cloudflare with it.
4. **Fill in the settings.** In the project's `deploy` folder, copy
   `.env.example` to `.env` and set:
   - `SITE_ADDRESS=http://:80`
   - `PUBLISH_IP=127.0.0.1`
   - `TUNNEL_TOKEN=` followed by the token
   - `BACKUP_DIR` set to a folder on another drive if you can, for example
     `D:/wallet-backups`.
5. **Start it** from the `deploy` folder:

       docker compose --profile tunnel up -d --build

6. **Create the first admin and check it**, as in steps 7 and 8 of section 2.

To try it on this computer only, without a domain, set `SITE_ADDRESS=localhost`
and open `https://localhost/api/ping`. The browser warns about the
certificate, because it is Caddy's own.

## 4. Backups

### What happens by itself

- A backup is made 10 seconds after the app starts, then **every hour**. It
  goes into `BACKUP_DIR` (`deploy/backups` unless you changed it) as
  `wallet-<date and time in UTC>.db.gz`, for example
  `wallet-20261018T050000Z.db.gz`.
- Every backup is checked before it is kept: the file must be sound, and the
  books are checked too. A backup that is damaged is not kept, and the next
  hour tries again. One whose books show a problem is kept, because it shows
  how things were, and the problem is reported.
- **Which backups are kept:**
  - every backup from the last 48 hours;
  - then the newest of each day, for 30 days;
  - then the newest of each month, for 12 months;
  - and always the newest one, however old.

  That is about 90 files at most. Old backups are removed only after a new
  one has been made, so a failing backup never thins out the good ones.
- On Linux, the database and the backups can only be read by the wallet's
  user (`WALLET_UID`): the files hold names, phone numbers, beneficiary IDs
  and password hashes.

### Get them off the machine

A backup on the same computer doesn't help when that computer is lost,
stolen, broken, or when the server is deleted. Keep a copy somewhere else, and
**encrypted** or somewhere only you can reach.

**On a server, use restic** (<https://restic.net>). It encrypts the backups
before they leave the machine, and can send them to Backblaze B2, Amazon S3,
any S3-compatible storage, or another computer over SFTP.

1. Install it with `sudo apt install restic`.
2. Make a storage bucket at the provider, and set up a restic repository
   there by following restic's documentation for that provider.
3. Write the repository password in a file, readable only by you
   (`chmod 600`). Keep a copy of that password **off the server**, on paper
   or in a password manager. Without it the backups can't be read.
4. Copy each new backup every hour, 15 minutes after the hour, with a line
   like this in `crontab -e` (with your repository and password file):

       15 * * * * RESTIC_REPOSITORY=<repository> RESTIC_PASSWORD_FILE=/home/you/.restic-password restic backup --quiet /srv/lyd-wallet/deploy/backups && RESTIC_REPOSITORY=<repository> RESTIC_PASSWORD_FILE=/home/you/.restic-password restic forget --quiet --keep-hourly 48 --keep-daily 30 --keep-monthly 12 --prune

   The storage provider's keys go in the same way; restic's documentation
   names them for each provider.

**On your own computer**, any of these works:

- restic (it runs on Windows too);
- an external drive as `BACKUP_DIR`, swapped every week with a second drive
  kept somewhere else;
- a synced folder (Dropbox, OneDrive, Google Drive), but only inside an
  encrypted vault such as Cryptomator.

Without Docker, the server's `--copy-to <folder>` option also copies each
backup into another folder.

### Know when something is wrong

Make a free check at <https://healthchecks.io> (or a similar service): a
period of 1 hour and a grace time of 1 hour. Put its ping address in `.env`
as `WALLET_PING_URL`, then run `docker compose up -d`.

After each hourly check and backup, the app calls that address. If something
went wrong, it calls the address with `/fail` added. The service emails you
when it hears `/fail`, and also when it stops hearing anything, for example
when the server is down.

### Practise a restore every month

A backup you have never restored is a hope, not a backup.

1. Copy the newest backup to another computer.
2. Check it there. This needs Python 3.10 or newer and this project's folder,
   nothing else:

       python3 -m app.manage check-backup wallet-20261018T050000Z.db.gz

3. Once in a while, restore it into a test database and look at a few
   balances:

       python3 -m app.manage restore wallet-20261018T050000Z.db.gz --db test.db

## 5. Restoring after a disaster

**The server is fine, but the database is damaged or wrong.** Run these from
the `deploy` folder:

    docker compose exec app manage list
    docker compose stop app
    docker compose run --rm app manage restore /backups/wallet-20261018T050000Z.db.gz
    docker compose start app

It asks you to type `restore`. It is refused while the app runs. The database
it replaces is not deleted: it is kept as
`/data/wallet.db.before-restore-<time>`.

**The server or computer is gone.**

1. Set up a new one as in section 2 or 3.
2. Get a backup back from the off-site copy (for restic:
   `restic restore latest --target /tmp/restored`, then copy the newest
   `.db.gz` into `BACKUP_DIR`).
3. Stop the app, restore that backup and start the app, as above.

**What a restore means:** the wallet goes back to the moment of that backup.
Transfers, deposits, payroll and orders recorded after it are not in it.
Enter them again from the receipts, payroll sheets and supplier messages, and
tell the workers. Workers who logged in after that moment log in again. This
is why the backups run every hour and are copied off the machine.

## 6. Updating the app

    docker compose exec app manage backup
    git pull
    docker compose pull --ignore-buildable
    docker compose up -d --build

Do this at least once a month: it also brings Python, Debian and Caddy
security fixes.

The database records which version of its tables it has. When a new version
of the app changes the tables, it upgrades the database by itself when it
starts, in one step. An older version of the app refuses to open a database
that a newer one has upgraded. To go back to an older version after an
upgrade, restore the backup taken just before it.

## 7. What to watch

- `docker compose ps`: the `app` line should say `healthy`.
- `docker compose logs --since 24h app | grep PROBLEM`: every line it prints
  needs looking at, such as a check of the books that found a difference, or
  a backup that failed.
- In the admin health answer (`GET /api/admin/health`), under `maintenance`:
  `last_check.ok`, `last_backup.ok`, and `backup_overdue` (true when no sound
  backup has been made for more than two hours and ten minutes).
- The healthchecks.io emails.
- Free disk space, with `df -h` on Linux.

## Without Docker

The wallet needs Python 3.10 or newer, and waitress for the server:

    pip install --require-hashes -r requirements.txt
    python3 -m app.serve --db /srv/wallet/wallet.db --backup-dir /srv/wallet/backups

Run Caddy on the same machine with the same `Caddyfile`, but with
`reverse_proxy 127.0.0.1:8000`. `python3 -m app.serve --help` lists every
option, and `python3 -m app.manage --help` lists the commands.
