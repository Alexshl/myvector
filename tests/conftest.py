import os
import subprocess
from pathlib import Path

import pytest

from myvec import config, embed, searchcmd

ORIG_TRIGGER = searchcmd.trigger_background_sync
TRIGGERED: list[tuple[str, str]] = []   # background syncs requested by the lazy freshness check

PHP = """<?php
namespace App;

class Invoice
{
    public function total(array $items): float
    {
        $sum = 0;
        foreach ($items as $i) { $sum += $i['price'] * $i['qty']; }
        return $sum;
    }

    public function renderPdf(): string
    {
        return 'pdf-document';
    }
}

function helper_vat_rate()
{
    return 0.25;
}
"""

JS = """import x from 'y';

function greetUser(name) {
  return 'hello ' + name;
}

const computeTax = (amount) => {
  return amount * 0.2;
};

class Cart {
  add(item) { this.items.push(item); }
}
"""

PY = '''import os

def pay_order(order):
    """Charge the customer."""
    return order.amount

class Gateway:
    def connect(self):
        return True

@staticmethod
def decorated_fn():
    return 1
'''

MD = """# Guide

Intro text about the billing guide, long enough to stand on its own as a chunk of documentation.

## Setup

Run the installer and configure the zebra settings. Make sure that the database is reachable first.

## Usage

Use the tool every day for invoices and payments. Reports are exported from the main menu on Fridays.
"""


def git(d, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(d), *args], check=True, capture_output=True, env=env).stdout.decode()


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MYVEC_HOME", str(home))
    monkeypatch.setenv("MYVEC_EMBEDDER", "fake")
    monkeypatch.setenv("MYVEC_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)   # tests must never see the real transcripts
    # nothing may touch the real user environment: HOME, XDG, global git config, LaunchAgents
    uh = tmp_path / "userhome"
    uh.mkdir()
    monkeypatch.setenv("HOME", str(uh))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(uh / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("MYVEC_LAUNCHAGENTS_DIR", str(tmp_path / "LaunchAgents"))
    monkeypatch.setenv("MYVEC_SYSTEMD_DIR", str(tmp_path / "systemd"))
    TRIGGERED.clear()
    monkeypatch.setattr(searchcmd, "trigger_background_sync", lambda project, root: TRIGGERED.append((project, root)))
    config.ensure_home()
    (home / "config.toml").write_text(config.default_config_text().replace("dims = 768", "dims = 64"))
    embed.STATS.update(texts=0, calls=0)
    yield home


@pytest.fixture
def ws(tmp_path):
    """Project `acme` (root is NOT a git repo) with sub-repos site (2 branches) and libs."""
    root = tmp_path / "acme"
    root.mkdir()
    site, libs = root / "site", root / "libs"
    for d in (site, libs):
        d.mkdir()
        git(d, "init", "-q", "-b", "main")
    write(site / "src/Invoice.php", PHP)
    write(site / "web/app.js", JS)
    write(site / "tools/pay.py", PY)
    write(site / "docs/guide.md", MD)
    write(site / ".env", "DB_PASSWORD=hunter2hunter2\n")
    write(site / "keys/notes.txt", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n")
    write(site / "vendor/lib/x.php", "<?php function vendored_thing() {}\n")
    write(libs / "common.py", "def libs_only_helper():\n    return 'quasar'\n")
    write(root / "README.md", "# Acme\n\nRoot level readme about the acme monorepo.\n")
    write(root / ".myvector.toml", 'name = "acme"\nshare_global = true\ninclude = []\nexclude = []\n')
    for d in (site, libs):
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "init")
    return root


def do_sync(root: Path):
    from myvec import db as dbmod
    from myvec import indexer, projects
    from myvec.embed import get_embedder

    cfg = config.load_config()
    db = dbmod.open_ready(cfg)
    st = indexer.sync_project(db, cfg, get_embedder(cfg), projects.find_marker(root))
    return st, db
