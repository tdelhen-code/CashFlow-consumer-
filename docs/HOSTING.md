# Hosting the LYD wallet

This guide is for the owner. It covers where to run the wallet, how to set it
up, how the backups work, and what to do when something goes wrong. The
commands alone are in [deploy/README.md](../deploy/README.md).

The wallet runs the same way on any computer with Docker:

- **the app**, which checks the books every hour and makes a backup every hour;
- **Caddy** in front of it, which gives the HTTPS address workers will open
  (for now only the API answers there: the screens are still to come);
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

The wallet has no screens yet. For now only its API, which the screens will
use, answers at its address: a browser that opens it shows
`{"error": "not found", "type": "http"}`. The screens for workers and admins
(Arabic, English, French) are still to come. You can already set up the
hosting and the backups.

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
   <https://docs.docker.com/engine/install/ubuntu/>. If you don't sign in as
   root, also let your user run Docker, then sign out and in again:

       sudo usermod -aG docker $USER

4. **Get the project** into `/srv/lyd-wallet` with git. Updates come the same
   way (section 6). If the repository is private, git asks you to sign in to
   GitHub: your user name, and a personal access token as the password.

       sudo apt install git
       sudo git clone https://github.com/tdelhen-code/CashFlow-consumer-.git /srv/lyd-wallet
       sudo chown -R "$USER:" /srv/lyd-wallet

5. **Fill in the settings:**

       cd /srv/lyd-wallet/deploy
       cp .env.example .env
       nano .env

   Set `SITE_ADDRESS=wallet.example.com`. Set `WALLET_UID` and `WALLET_GID`
   to the numbers that `id -u` and `id -g` print, so the backup files belong
   to you. If `id -u` prints 0 (you are root), leave them at 10001: root can
   read the backups anyway.
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
9. **Set up the off-site backups and both alerts** (section 4). Don't skip
   this.

## 3. On your own computer

1. **Install Docker Desktop** (on Windows, with the WSL 2 option). In its
   settings, have it start when you sign in.
2. **Keep the computer on.** In the power settings, set it never to sleep.
   Use a UPS.

   On Windows, Docker Desktop only runs once someone has signed in. After
   every restart, such as a Windows Update or a power cut longer than the UPS
   lasts, the wallet stays down until someone signs in. So:
   - have Windows sign in by itself, for example with Sysinternals Autologon
     (<https://learn.microsoft.com/sysinternals/downloads/autologon>), and
     have the screen lock after a minute (a screen saver with "On resume,
     display logon screen"), so nobody else can use the computer;
   - in Windows Update, set the active hours to the hours workers use the
     wallet, so it restarts outside them;
   - in the computer's BIOS (or UEFI) settings, set "Restore on AC power
     loss" (the name varies) to "Power on", so it starts by itself when the
     power comes back;
   - count on the healthchecks.io alert (section 4): it emails you when the
     wallet has stopped.
3. **Set up a Cloudflare Tunnel.** Make a free Cloudflare account and add
   your domain to it (Cloudflare then runs the domain's DNS). In the
   Cloudflare dashboard:
   - create a tunnel of the *cloudflared* kind, and copy its **token**;
   - add a public hostname such as `wallet.example.com`, with the service
     `http://caddy:80`.

   Cloudflare gives the HTTPS. The tunnel works because Cloudflare decrypts
   the traffic on its way through, so this means trusting Cloudflare with it.
4. **Get the project** with git, into a folder of your own. On Windows,
   install Git for Windows first (<https://git-scm.com/download/win>), then
   open PowerShell: it starts in your own user folder. On a Mac or Linux,
   open a terminal. Type:

       git clone https://github.com/tdelhen-code/CashFlow-consumer-.git lyd-wallet

   If the repository is private, git asks you to sign in to GitHub.
5. **Fill in the settings.** Copy `.env.example` to `.env` in the project's
   `deploy` folder, and open it. On Windows:

       cd lyd-wallet\deploy
       copy .env.example .env
       notepad .env

   (On a Mac or Linux: `cd lyd-wallet/deploy`, `cp .env.example .env` and
   `nano .env`.) Set:
   - `SITE_ADDRESS=http://:80`
   - `PUBLISH_IP=127.0.0.1`
   - `TUNNEL_TOKEN=` followed by the token
   - `BACKUP_DIR` set to a folder on another drive that only you use, if you
     can, for example `D:/wallet-backups`.
6. **Start it** from the `deploy` folder:

       docker compose --profile tunnel up -d --build

7. **Create the first admin and check it**, as in steps 7 and 8 of section 2.

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
  - and always the 48 newest, whatever their dates.

  That is about 90 files at most. Old backups are removed only after a new
  one has been made, so a failing backup never thins out the good ones.
  Nothing else in the folder is removed, apart from what an interrupted
  backup left behind.
- On Linux, with or without Docker, only the user the wallet runs as
  (`WALLET_UID` with Docker) and root can read the database and the backups:
  the files hold names, phone numbers, beneficiary IDs and password hashes.
  On Windows, anyone who can open the backup folder can read them, so keep it
  in your own user folder or on a drive only you use.

### Get them off the machine

A backup on the same computer doesn't help when that computer is lost,
stolen, broken, or when the server is deleted. Keep a copy somewhere else, and
**encrypted** or somewhere only you can reach.

**On a server, use restic** (<https://restic.net>). It encrypts the backups
before they leave the machine, and can send them to Backblaze B2, Amazon S3,
any S3-compatible storage, or another computer over SFTP. Set it up as the
user from section 2 (the one whose numbers are in `WALLET_UID` and
`WALLET_GID`, or root), so it can read the backups. `~` is that user's home
folder.

1. Install it: `sudo apt install restic curl`.
2. Make a storage bucket at the provider. Restic's documentation
   (<https://restic.readthedocs.io>) gives, for each provider, the address of
   the repository and the names of the keys it needs.
3. Write the repository password (a long one of your own) in a file that
   only you can read:

       nano ~/.restic-password
       chmod 600 ~/.restic-password

4. Write the settings in another file, with `nano ~/.restic-env`. For
   example, for Amazon S3 or most S3-compatible storage:

       export RESTIC_REPOSITORY=s3:https://<storage address>/<bucket>
       export RESTIC_PASSWORD_FILE="$HOME/.restic-password"
       export AWS_ACCESS_KEY_ID=<key ID>
       export AWS_SECRET_ACCESS_KEY=<secret key>

   Make it readable only by you, and create the repository:

       chmod 600 ~/.restic-env
       . ~/.restic-env
       restic init

   Keep a copy of the password **and** of these settings **off the server**,
   on paper or in a password manager. Without the password the backups can't
   be read, and without the settings they can't be found.
5. Make a second check at healthchecks.io for this copy, with a period of 1
   hour and a grace time of 1 hour (see "Know when something is wrong"
   below). Write this script in `~/wallet-offsite.sh`, with that check's
   ping address on the last line:

       #!/bin/sh
       # The off-site copy; healthchecks.io hears of it only when restic worked.
       set -e
       . "$HOME/.restic-env"
       restic backup --quiet /srv/lyd-wallet/deploy/backups
       restic forget --quiet --keep-hourly 48 --keep-daily 30 --keep-monthly 12 --prune
       curl -fsS -m 10 --retry 5 -o /dev/null https://hc-ping.com/<second-uuid>

   (with your folder in place of `/srv/lyd-wallet/deploy/backups` if you
   changed `BACKUP_DIR`). Make it runnable by you only, and try it once: the
   second check should turn green.

       chmod 700 ~/wallet-offsite.sh
       ~/wallet-offsite.sh

6. Have it run every hour, 15 minutes after the hour: type `crontab -e` and
   add this line.

       15 * * * * $HOME/wallet-offsite.sh

**On your own computer**, any of these works:

- restic (it runs on Windows too);
- an external drive as `BACKUP_DIR`, swapped every week with a second drive
  kept somewhere else;
- a synced folder (Dropbox, OneDrive, Google Drive), but only inside an
  encrypted vault such as Cryptomator.

Without Docker, the server's `--copy-to <folder>` option also copies each
backup into another folder. That folder must already be there: make it once
on the drive. When it is missing, for example because the drive isn't
plugged in, the copy is reported as a problem; it is never made on this
computer's own disk instead.

### Know when something is wrong

Make a free check at <https://healthchecks.io> (or a similar service): a
period of 1 hour and a grace time of 1 hour. Put its ping address in `.env`
as `WALLET_PING_URL`, then run `docker compose up -d` (behind the tunnel,
`docker compose --profile tunnel up -d`).

After each hourly check and backup, the app calls that address. If something
went wrong, it calls the address with `/fail` added. The service emails you
when it hears `/fail`, and also when it stops hearing anything, for example
when the server is down.

The **second check** is for the off-site copy (restic, above). The script
calls it only when restic has copied the backups, so the service emails you
when the off-site copy stops working, for example when the storage key no
longer works or the bucket is full.

### Practise a restore every month

A backup you have never restored is a hope, not a backup.

1. On the server, check the off-site copy. The last copy listed should be
   from the last hour.

       . ~/.restic-env
       restic snapshots

2. Copy the newest backup to another computer.
3. Check it there. This needs Python 3.10 or newer and this project's folder,
   nothing else. Run it from the project's folder (on Windows, type `py` in
   place of `python3`):

       python3 -m app.manage check-backup wallet-20261018T050000Z.db.gz

4. Once in a while, restore it into a test database, and check that the books
   add up:

       python3 -m app.manage restore wallet-20261018T050000Z.db.gz --db test.db
       python3 -m app.manage check --db test.db

## 5. Restoring after a disaster

**The server is fine, but the database is damaged or wrong.** Run these from
the `deploy` folder:

    docker compose stop app
    docker compose run --rm app manage list
    docker compose run --rm app manage restore /backups/wallet-20261018T050000Z.db.gz
    docker compose start app

The list shows the backups, newest first. It works however damaged the
database is, as it doesn't open it. The names are also those of the files in
`BACKUP_DIR`. The restore asks you to type `restore`. It is refused while the
app runs. The database it replaces is not deleted: it is kept as
`/data/wallet.db.before-restore-<time>`.

**The server or computer is gone.** Don't start the wallet on the new one
before the restore: within 10 seconds it would make a backup of an empty
wallet.

1. **Set up a new one**, but only steps 1 to 5 of section 2 (or of section
   3), up to filling in `.env`. Don't start the app, and don't set up the
   off-site copy yet.
2. **Get the backups back.** With restic, on the new server: install it, put
   back `~/.restic-password` and `~/.restic-env` from your copies (section 4,
   steps 3 and 4), then load the settings and list the copies:

       sudo apt install restic curl
       . ~/.restic-env
       restic snapshots

   Each line is one copy: its ID, its time, and its host (the name of the
   computer that made it). Take the newest one from the old server, and get
   it back by its ID:

       restic restore <ID> --target /tmp/restored

   (or `restic restore latest --host <old server name> --target /tmp/restored`).
   The files come back under their full old path: the backups are in
   `/tmp/restored/srv/lyd-wallet/deploy/backups`.

   Without restic, get the backups from wherever your off-site copy is (the
   second drive, the synced folder).
3. **Put them in `BACKUP_DIR`**, and find the newest one: the one with the
   latest date and time (UTC) in its name. Check that it is from shortly
   before the old server was lost.

       mkdir -p /srv/lyd-wallet/deploy/backups
       cp /tmp/restored/srv/lyd-wallet/deploy/backups/*.db.gz /srv/lyd-wallet/deploy/backups/
       ls /srv/lyd-wallet/deploy/backups

4. **Restore it and start the wallet**, from the `deploy` folder, with that
   backup's name:

       docker compose build
       docker compose run --rm app manage restore /backups/wallet-20261018T050000Z.db.gz
       docker compose up -d

   Behind the tunnel, the last line is `docker compose --profile tunnel up -d`.
   Don't create a first admin: the admins are in the backup.
5. **Check it:** `https://wallet.example.com/api/ping` shows `{"ok": true}`,
   and `docker compose exec app manage check` finds nothing wrong.
6. **Set up the off-site copy and both alerts** as in section 4 (the same
   restic repository and the same two checks), and do what "What a restore
   means" below says.

**When the app won't start because its database is missing.** When the
database is gone but `BACKUP_DIR` has backups of it (for example when the
Docker volume was removed), the app refuses to start, so it can't begin an
empty wallet whose backups would push out the real ones.
`docker compose logs app` says so. Restore one, as in the first case above.
To start a new, empty wallet on purpose, use an empty backup folder.

**What a restore means:** the wallet goes back to the moment of that backup.

- Everyone logs in again: the restore ends every session. Setup codes given
  out before the backup no longer work: give new ones to whoever still needs
  one.
- Blocks, deactivations, password and setup-code changes, new accounts,
  suppliers, prices and other admin changes made after the backup are
  undone. Do them again **before** you tell the workers the wallet is back,
  above all the block of a stolen phone's account. A worker who changed their
  password after the backup has their old one again.
- Transfers, deposits, payroll and orders recorded after it are not in it.
  Enter them again from the receipts, payroll sheets and supplier messages,
  and tell the workers.

This is why the backups run every hour and are copied off the machine.

## 6. Updating the app

    docker compose exec app manage backup
    git pull
    docker compose pull --ignore-buildable
    docker compose up -d --build

Behind the tunnel, add `--profile tunnel` to the last two lines. Don't add it
on a rented server: it would start the tunnel there, with no token.

    docker compose exec app manage backup
    git pull
    docker compose --profile tunnel pull --ignore-buildable
    docker compose --profile tunnel up -d --build

Do this at least once a month: it also brings Python, Debian and Caddy
security fixes. Behind the tunnel, every few months also set
`CLOUDFLARED_VERSION` in `.env` to the newest release at
<https://github.com/cloudflare/cloudflared/releases> before you update.

The database records which version of its tables it has. When a new version
of the app changes the tables, it upgrades the database by itself when it
starts, in one step. An older version of the app refuses to open a database
that a newer one has upgraded. To go back to an older version after an
upgrade, restore the backup taken just before it.

## 7. What to watch

- `docker compose ps`: the `app` line should say `healthy`.
- `docker compose logs --since 24h app | grep PROBLEM` (on Windows:
  `docker compose logs --since 24h app | findstr PROBLEM`): every line it
  prints needs looking at, such as a check of the books that found a
  difference, or a backup that failed or could not be copied.
- The emails from both healthchecks.io checks: the app's, and the off-site
  copy's.
- In the admin health answer (`GET /api/admin/health`, for now an API call
  with an admin's login), under `maintenance`: `last_check.ok`,
  `last_backup.ok` (also false when a copy or the removal of old backups
  failed), and `backup_overdue` (true when no sound backup has been made for
  more than two hours and ten minutes). The same is in the `PROBLEM` lines of
  the log.
- Free disk space, with `df -h` on Linux.

## Without Docker

The wallet needs Python 3.10 or newer, and waitress for the server. Put
waitress in a virtual environment in the project's folder (Ubuntu's own pip
refuses to install it for the whole system), and run the wallet as a user of
its own, not as root:

    cd /srv/lyd-wallet
    sudo apt install python3-venv
    python3 -m venv .venv
    .venv/bin/pip install --require-hashes -r requirements.txt
    sudo adduser --system --group --home /srv/wallet wallet
    sudo -u wallet .venv/bin/python -m app.serve --db /srv/wallet/wallet.db --backup-dir /srv/wallet/backups

The wallet makes its files readable by that user only. Run Caddy on the same
machine (from Caddy's own package, <https://caddyserver.com/docs/install>:
this Caddyfile needs Caddy 2.7 or newer), with a copy of `deploy/Caddyfile`
as `/etc/caddy/Caddyfile` and two changes in it: your domain in place of
`{$SITE_ADDRESS}` (Caddy's own service has no such setting), and
`reverse_proxy 127.0.0.1:8000` in place of `reverse_proxy app:8000`. A
`--copy-to` folder must already be there (section 4).
`.venv/bin/python -m app.serve --help` lists every option, and
`.venv/bin/python -m app.manage --help` lists the commands.
