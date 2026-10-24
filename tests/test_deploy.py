"""The deploy files and the owner's guides: what they run, and what they tell
the owner to type."""
import fnmatch
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from app import manage as M
from app import serve as S
from app import web as W

ROOT = Path(__file__).resolve().parent.parent
SH = shutil.which("sh")


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def between(text, start, end):
    """The part of `text` after `start`, up to the next `end`."""
    if start not in text:
        raise AssertionError(f"not found: {start!r}")
    return text.split(start, 1)[1].split(end, 1)[0]


def code_blocks(text):
    """The indented code blocks of a Markdown text, as lists of lines. A block
    starts after a blank line, so an indented line of a list item isn't one."""
    blocks, in_block, blank = [], False, True
    for line in text.splitlines():
        code = line.startswith("    ") and bool(line.strip()) and (in_block or blank)
        if code and not in_block:
            blocks.append([])
        if code:
            blocks[-1].append(line.strip())
        in_block, blank = code, not line.strip()
    return blocks


def command_words(text):
    """The words of each command line in the code blocks, split as sh would."""
    return [words for block in code_blocks(text) for line in block
            if (words := shlex.split(line, comments=True))]


def commands(text):
    """Each command line of the code blocks, without its comment."""
    return [" ".join(words) for words in command_words(text)]


def flat(text):
    """`text` on one line: a line break, an indent or the # of a comment line
    doesn't split a sentence."""
    return " ".join(text.replace("\n#", "\n").split())


def stubs(folder, **programs):
    """Small sh scripts standing in for programs, in `folder` (for PATH)."""
    for name, body in programs.items():
        path = Path(folder) / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
    return str(folder)


class EntrypointTests(unittest.TestCase):
    @unittest.skipUnless(SH and os.name != "nt", "needs a POSIX sh")
    def test_the_wallet_never_runs_as_root(self):
        # WALLET_UID from `id -u` as root is 0: the wallet runs as 10001
        # then, says so, and still starts (a refusal would only loop).
        with tempfile.TemporaryDirectory() as tmp:
            path = stubs(tmp, id="echo 0", chown="exit 0", chmod="exit 0",
                         setpriv='echo "$@"')
            for ids, user, said in (({"WALLET_UID": "0", "WALLET_GID": "0"}, "10001:10001", True),
                                    ({"WALLET_UID": "1000", "WALLET_GID": "0"}, "1000:10001", True),
                                    ({"WALLET_UID": "1000", "WALLET_GID": "1000"}, "1000:1000", False),
                                    ({}, "10001:10001", False)):
                with self.subTest(ids=ids):
                    done = subprocess.run(
                        [SH, str(ROOT / "deploy" / "entrypoint.sh"), "python", "-m", "app.serve"],
                        env={"PATH": path, **ids}, capture_output=True, text=True, timeout=30)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    uid, gid = user.split(":")
                    self.assertEqual(done.stdout.split(), [
                        f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--",
                        "python", "-m", "app.serve"])
                    self.assertEqual(len(done.stderr.splitlines()), 1 if said else 0, done.stderr)
        for name in ("docs/HOSTING.md", "deploy/.env.example"):
            self.assertIn("prints 0 (you are root), leave them at 10001", flat(read(name)), name)


class ComposeTests(unittest.TestCase):
    def test_an_update_brings_new_base_and_tunnel_images(self):
        compose = read("deploy/compose.yaml")
        self.assertIn("    build:\n      context: ..\n      pull: true", compose)
        self.assertIn("image: cloudflare/cloudflared:${CLOUDFLARED_VERSION:-", compose)
        self.assertIn("\nCLOUDFLARED_VERSION=\n", read("deploy/.env.example"))
        # Behind the tunnel the update pulls and starts cloudflared too.
        for text in (between(read("docs/HOSTING.md"), "## 6.", "## 7."),
                     between(read("deploy/README.md"), "## Update", "## Settings")):
            self.assertIn("docker compose --profile tunnel pull --ignore-buildable", text)
            self.assertIn("docker compose --profile tunnel up -d --build", text)
            self.assertIn("CLOUDFLARED_VERSION", text)

    @unittest.skipUnless(shutil.which("docker"), "needs docker compose")
    def test_compose_takes_the_example_settings(self):
        def config(**env):
            done = subprocess.run(
                ["docker", "compose", "-f", str(ROOT / "deploy" / "compose.yaml"),
                 "--env-file", str(ROOT / "deploy" / ".env.example"), "--profile", "tunnel",
                 "config", "--format", "json"],
                env={k: v for k, v in os.environ.items() if k != "CLOUDFLARED_VERSION"} | env,
                capture_output=True, text=True, timeout=60)
            if done.returncode and "is not a docker command" in done.stderr:
                self.skipTest("needs docker compose")
            self.assertEqual(done.returncode, 0, done.stderr)
            return json.loads(done.stdout)["services"]
        services = config()
        self.assertIs(services["app"]["build"]["pull"], True)
        self.assertEqual(services["cloudflared"]["image"], "cloudflare/cloudflared:2026.9.3")
        self.assertEqual(config(CLOUDFLARED_VERSION="2027.1.0")["cloudflared"]["image"],
                         "cloudflare/cloudflared:2027.1.0")


class GitignoreTests(unittest.TestCase):
    def test_backups_and_their_leftovers_stay_out_of_git(self):
        if not shutil.which("git") or subprocess.run(
                ["git", "rev-parse"], cwd=ROOT, capture_output=True).returncode:
            self.skipTest("needs a git checkout")

        def ignored(path):
            return subprocess.run(["git", "check-ignore", "-q", "--no-index", path],
                                  cwd=ROOT, capture_output=True).returncode == 0
        # An interrupted backup holds the start of the database.
        for path in ("backups/wallet-20261018T050000Z.db.gz.k3j9x2.partial",
                     "wallet-20261018T050000Z.db.gz.partial",
                     "backups/.tmp-wallet-backup-k3j9x2/wallet.db", "backups/notes.txt",
                     "deploy/backups/wallet-20261018T050000Z.db.gz", ".venv/bin/python"):
            self.assertTrue(ignored(path), path)
        for path in ("app/backup.py", "docs/HOSTING.md", "deploy/compose.yaml"):
            self.assertFalse(ignored(path), path)


class GuideTests(unittest.TestCase):
    """docs/HOSTING.md and deploy/README.md: the commands work as typed."""

    def setUp(self):
        self.hosting = read("docs/HOSTING.md")
        self.deploy = read("deploy/README.md")

    def section(self, number):
        return between(self.hosting, f"\n## {number}.", "\n## ")

    def test_every_manage_and_serve_command_is_one_they_take(self):
        settings = {"WALLET_DB": "/data/wallet.db", "WALLET_BACKUP_DIR": "/backups"}
        programs = {"manage": M._parser, "app.manage": M._parser, "app.serve": S._parser}
        checked = 0
        for name, text in (("HOSTING", self.hosting), ("deploy/README", self.deploy)):
            for words in command_words(text):
                program = next((w for w in words if w in programs), None)
                if program is None:
                    continue
                argv = words[words.index(program) + 1:]
                checked += 1
                with self.subTest(name=name, words=words), mock.patch.dict(os.environ, settings), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                    try:
                        programs[program]().parse_args(argv)
                    except SystemExit as e:
                        self.assertEqual(e.code, 0, err.getvalue())
        self.assertGreater(checked, 10)

    def test_a_restore_never_needs_the_app_running(self):
        # A damaged or too new database stops the app, and exec needs it running.
        for part in (between(self.hosting, "**The server is fine", "**The server or computer"),
                     between(self.deploy, "## Restore a backup", "\n## ")):
            steps = commands(part)
            self.assertEqual(steps[:2], ["docker compose stop app",
                                         "docker compose run --rm app manage list"])
            self.assertEqual([s for s in steps if " exec " in s], [])
            self.assertIn("are also those of the files in `BACKUP_DIR`", flat(part))

    def test_a_lost_server_gets_the_old_backups_back_before_the_app_starts(self):
        gone = between(self.hosting, "**The server or computer is gone.**", "**When the app")
        steps = commands(gone)
        restore = next(i for i, s in enumerate(steps) if "manage restore /backups/" in s)
        started = [i for i, s in enumerate(steps) if s.startswith("docker compose") and " up" in s]
        self.assertTrue(started)
        self.assertLess(restore, min(started))          # its first backup would be empty
        self.assertFalse([s for s in steps if "first-admin" in s])
        self.assertIn("only steps 1 to 5 of section 2", flat(gone))
        # restic needs its settings; the copy is the old server's, not the newest
        self.assertLess(steps.index(". ~/.restic-env"), steps.index("restic snapshots"))
        self.assertIn("restic restore <ID> --target /tmp/restored", steps)
        for latest in re.findall(r"restic restore latest[^`\n]*", gone):
            self.assertIn("--host", latest)
        self.assertIn("/tmp/restored/srv/lyd-wallet/deploy/backups", gone)
        # the same domain, tunnel, repository and checks as the old server
        for said in ("change its A record to the new server's IPv4 address instead of "
                     "adding a second one", "copy its token again, instead of making a new "
                     "tunnel", "don't run `restic init`: the repository is already there",
                     "Don't run `restic init` or make new checks"):
            self.assertIn(said, flat(gone))
        self.assertNotIn("restic init", steps)
        # the app refuses to start on backups without their database
        missing = flat(between(self.hosting, "**When the app won't start", "**What a restore"))
        self.assertIn("refuses to start", missing)
        self.assertIn("empty backup folder", missing)

    def test_what_a_restore_undoes_is_said(self):
        means = flat(between(self.hosting, "**What a restore means:**", "\n## "))
        for said in ("Everyone logs in again", "Setup codes given out before the backup no "
                     "longer work", "Blocks, deactivations, password and setup-code changes",
                     "**before** you tell the workers", "stolen phone",
                     "Transfers, deposits, payroll and orders"):
            self.assertIn(said, means)

    @unittest.skipUnless(SH and os.name != "nt", "needs a POSIX sh")
    def test_the_offsite_copy_pings_only_when_restic_worked(self):
        offsite = between(self.hosting, "### Get them off the machine", "### Know when")
        script = next(b for b in code_blocks(offsite) if b[0] == "#!/bin/sh")
        self.assertIn("15 * * * * $HOME/wallet-offsite.sh", commands(offsite))
        with tempfile.TemporaryDirectory() as home:
            Path(home, ".restic-env").write_text("export RESTIC_REPOSITORY=test\n")
            path = Path(home, "wallet-offsite.sh")
            path.write_text(re.sub(r"<[^>]*>", "test", "\n".join(script)) + "\n")
            bin_ = Path(home, "bin")
            bin_.mkdir()
            stubs(bin_, restic='echo "restic $1" >> "$HOME/calls"; [ "$1" != "$FAIL" ]',
                  curl='echo curl >> "$HOME/calls"')
            for fail, calls in (("", ["restic backup", "restic forget", "curl"]),
                                ("backup", ["restic backup"]),
                                ("forget", ["restic backup", "restic forget"])):
                with self.subTest(fail=fail):
                    Path(home, "calls").write_text("")
                    done = subprocess.run([SH, str(path)], capture_output=True, text=True,
                                          env={"PATH": str(bin_), "HOME": home, "FAIL": fail},
                                          timeout=30)
                    self.assertEqual(done.returncode != 0, bool(fail), done.stderr)
                    self.assertEqual(Path(home, "calls").read_text().splitlines(), calls)
        self.assertIn("both healthchecks.io checks", flat(self.section(7)))
        self.assertIn("restic snapshots", commands(between(
            self.hosting, "### Practise a restore", "\n## ")))

    def test_who_can_read_the_backups_is_said_for_each_system(self):
        kept = flat(between(self.hosting, "### What happens by itself", "### "))
        self.assertIn("On Linux, with or without Docker, only the user the wallet runs as", kept)
        self.assertIn("On Windows, anyone who can open the backup folder can read them", kept)
        self.assertIn("always the 48 newest", kept)

    def test_without_docker_waitress_goes_in_a_virtual_environment(self):
        # The system's pip refuses on Ubuntu 24.04 (PEP 668).
        for name in ("docs/HOSTING.md", "deploy/README.md", "README.md", "requirements.txt"):
            for line in read(name).splitlines():
                if "pip install" in line:
                    self.assertIn(".venv/bin/pip install", line, name)
        without = between(self.hosting, "## Without Docker", "\n## ")
        self.assertIn("python3 -m venv .venv", commands(without))
        self.assertIn("in place of `{$SITE_ADDRESS}`", flat(without))
        self.assertIn("`reverse_proxy 127.0.0.1:8000` in place of", flat(without))
        self.assertIn("folder must already be there", flat(self.section(4)))

    def test_without_docker_manage_runs_as_the_wallet_user(self):
        # The wallet's files are for that user only: another can't open them,
        # and a restore run as root leaves a database the server can't open.
        without = between(self.hosting, "## Without Docker", "\n## ")
        manage = [s for s in commands(without) if "app.manage" in s]
        self.assertTrue(manage)
        for command in manage:
            self.assertTrue(command.startswith("sudo -u wallet "), command)
        self.assertIn("`/srv/wallet/backups` in place of `/srv/lyd-wallet/deploy/backups`",
                      flat(without))

    def test_the_project_is_cloned_so_it_can_be_updated(self):
        clone = "git clone https://github.com/tdelhen-code/CashFlow-consumer-.git"
        for number in (2, 3):
            self.assertTrue([s for s in commands(self.section(number)) if clone in s], number)
        self.assertNotIn("copying the folder", self.hosting)
        self.assertIn("git pull", commands(self.section(6)))

    def test_the_log_check_works_on_windows_too(self):
        for text in (self.hosting, self.deploy):
            self.assertIn("grep PROBLEM", text)
            self.assertIn("findstr PROBLEM", text)

    def test_windows_needs_someone_signed_in(self):
        own = flat(self.section(3))
        self.assertIn("Docker Desktop only runs once someone has signed in", own)
        for advice in ("Autologon", "active hours", "Restore on AC power loss", "healthchecks.io"):
            self.assertIn(advice, own)

    def test_the_guide_says_which_screens_there_are(self):
        intro = flat(between(self.hosting, "The wallet runs the same way", "\n## 1."))
        self.assertIn("(the workers' screens and the API; the admin screens are still to come)",
                      intro)
        where = flat(self.section(1))
        self.assertIn("The workers' screens are at the wallet's address", where)
        self.assertIn("The admin screens are still to come", where)
        self.assertNotIn("no screens yet", self.hosting)
        self.assertNotIn("look at a few balances", self.hosting)
        self.assertIn("python3 -m app.manage check --db test.db", commands(self.hosting))
        self.assertIn("for now an API call with an admin's login", flat(self.section(7)))

    def test_the_tunnel_keeps_the_address_and_the_page_as_they_are(self):
        # The wallet refuses a browser's request whose Host is not the address
        # the browser opened, and its page refuses scripts added on the way.
        own = flat(self.section(3))
        for said in ("*HTTP Host Header*", "*Rocket Loader*", "*Web Analytics*"):
            self.assertIn(said, own)

    def test_behind_the_tunnel_plain_http_goes_to_https(self):
        # Caddy serves plain http behind the tunnel; a page opened over http
        # would send the password unencrypted (and every login be refused).
        caddy = read("deploy/Caddyfile")
        site = caddy[caddy.index("{$SITE_ADDRESS} {"):]
        self.assertIn('@plain_http header Cf-Visitor `*"scheme":"http"*`', site)
        self.assertIn("redir @plain_http https://{host}{uri} 308", site)
        self.assertLess(site.index("redir @plain_http"), site.index("reverse_proxy"))
        own = flat(self.section(3))
        self.assertIn("*SSL/TLS > Edge Certificates*, turn on *Always Use HTTPS*", own)
        self.assertIn("the browser should end up at `https://wallet.example.com/`", own)


class ImageTests(unittest.TestCase):
    def test_the_image_ships_the_screens(self):
        # app/static is in the image only through `!app/` and `COPY app/ app/`:
        # a slimmer ignore file would ship an image whose / is a JSON 404.
        patterns = [line.strip() for line in read(".dockerignore").splitlines()
                    if line.strip() and not line.startswith("#")]
        self.assertIn("!app/", patterns)
        for pattern in patterns[patterns.index("!app/") + 1:]:     # later lines win
            if pattern.startswith("!"):
                continue
            for path in ("app/static", *(f"app/static/{n}" for n in W.STATIC_FILES.values())):
                self.assertFalse(fnmatch.fnmatchcase(path,
                                                     pattern.rstrip("/").replace("**", "*")),
                                 (pattern, path))
        self.assertIn("COPY app/ app/", read("Dockerfile").splitlines())


if __name__ == "__main__":
    unittest.main()
