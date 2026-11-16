import json

import pytest
from typer.testing import CliRunner

from conftest import do_sync, git, write
from myvec import filters
from myvec.commands import app

runner = CliRunner()


@pytest.mark.parametrize("src,kept,masked", [
    ("$token = $this->getToken();", "$token = $this->getToken();", 0),
    ("$token = getenv('API_TOKEN');", "$token = getenv('API_TOKEN');", 0),
    ("'secret' => env('APP_SECRET'),", "'secret' => env('APP_SECRET'),", 0),
    ("'pwd' => config('db.password'),", "'pwd' => config('db.password'),", 0),
    ("self.token = refresh_token", "self.token = refresh_token", 0),
    ("token: string;", "token: string;", 0),
    ("password = os.environ['DB_PASS']", "password = os.environ['DB_PASS']", 0),
    ('"password": "${DB_PASSWORD}"', '"password": "${DB_PASSWORD}"', 0),
    ("'password' => 'hunter22',", "'password' => '•••',", 1),
    ('"api_key": "abcd1234efgh"', '"api_key": "•••"', 1),
    ('$password = "s3cr3t-value";', '$password = "•••";', 1),
    ("DB_PASSWORD=hunter2hunter2", "DB_PASSWORD=•••", 1),
    ("password: hunter2hunter2", "password: •••", 1),
    ("SECRET=abc-def-ghi\nAPI_KEY: abcdef123456", "SECRET=•••\nAPI_KEY: •••", 2),
])
def test_mask_secrets(src, kept, masked):
    out, n = filters.mask_secrets(src)
    assert (out, n) == (kept, masked)
    assert "hunter" not in out and "s3cr3t" not in out


def test_hard_secrets_detected():
    pem = "x = 1\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"
    for t in (pem, "key = 'AKIAIOSFODNN7EXAMPLE'", "sk_live_abcdefgh12345678", "ghp_" + "a" * 36, "xoxb-1234567890-abcdefg",
              "AIza" + "b" * 35, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"):
        assert filters.has_hard_secret(t)
    assert not filters.has_hard_secret("$token = $this->getToken();")


def test_sync_masks_and_drops(ws):
    write(ws / "site/src/Config.php", """<?php
class Config
{
    public function boot()
    {
        $token = $this->getToken();
        $conf = ['password' => 'hunter22', 'host' => 'db.local'];
        return $conf;
    }
}
""")
    write(ws / "site/src/Aws.php", "<?php\nfunction aws_client_zq()\n{\n    return new Client('AKIAIOSFODNN7EXAMPLE');\n}\n")
    write(ws / "site/src/Pem.py", 'def pem_holder():\n    return """-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"""\n')
    git(ws / "site", "add", "-A")
    git(ws / "site", "commit", "-q", "-m", "secrets")
    st, db = do_sync(ws)
    assert st.secrets_masked == 1
    assert st.secrets_skipped == 3  # notes.txt PEM (fixture) + Aws.php + Pem.py
    texts = " ".join(r[0] for r in db.execute("SELECT text FROM chunks"))
    assert "$token = $this->getToken();" in texts          # ordinary code is kept
    assert "'password' => '•••'" in texts and "hunter22" not in texts
    assert "AKIAIOSFODNN7EXAMPLE" not in texts and "PRIVATE KEY" not in texts
    assert db.execute("SELECT count(*) FROM chunks WHERE text LIKE '%aws_client_zq%'").fetchone()[0] == 0
    # masked text is what is searchable (FTS) too
    assert db.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'hunter22'").fetchone()[0] == 0
    assert db.execute("SELECT sum(secret_masked) FROM blobs").fetchone()[0] == 1


def test_status_shows_masked_and_dropped(ws, monkeypatch):
    write(ws / "site/src/Config.php", "<?php\n$conf = ['password' => 'hunter22'];\n")
    git(ws / "site", "add", "-A")
    git(ws / "site", "commit", "-q", "-m", "c")
    do_sync(ws)
    r = runner.invoke(app, ["status"])
    assert "masked=1" in r.stdout and "dropped=1" in r.stdout
    assert "masked secret values: 1" in r.stdout and "skipped secret chunks: 1" in r.stdout


def test_remember_masks_and_refuses(ws, env, monkeypatch):
    monkeypatch.chdir(ws / "site")
    do_sync(ws)
    r = runner.invoke(app, ["remember", "the staging db uses password = 'hunter22xyz' on host db1"])
    assert r.exit_code == 0 and "masked" in r.stdout
    f = next((env / "vault/projects/acme").glob("*.md"))
    assert "hunter22xyz" not in f.read_text() and "•••" in f.read_text()
    for bad in ("key AKIAIOSFODNN7EXAMPLE", "my key:\n-----BEGIN RSA PRIVATE KEY-----\nabc", "ghp_" + "a" * 36):
        r = runner.invoke(app, ["remember", bad])
        assert r.exit_code != 0 and "secret" in r.output
    assert len(list((env / "vault/projects/acme").glob("*.md"))) == 1
