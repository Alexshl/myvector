import pytest

from myvec import chunk, filters
from conftest import JS, MD, PHP, PY


@pytest.mark.parametrize("p", [
    "vendor/a.php", "a/node_modules/x.js", ".env", ".env.local", ".dev.vars", "worker/.dev.vars.staging", "config/secrets.yml", "a/my_password.txt",
    "k.pem", "k.key", "id_rsa", "dump.sql", "a.sqlite", "app.min.js", "composer.lock", "logo.png",
    "dist/x.js", "build/x.js", "templates_c/x.php", "graphify-out/graph.json", ".git/config", ".cache/x", "img/logo.SVG",
])
def test_denied(p):
    assert filters.is_denied_path(p)


@pytest.mark.parametrize("p", ["src/a.php", "README.md", "graphify-out/GRAPH_REPORT.md", ".github/workflows/ci.yml"])
def test_allowed(p):
    assert not filters.is_denied_path(p)


@pytest.mark.parametrize("t", [
    "-----BEGIN RSA PRIVATE KEY-----", "key AKIAIOSFODNN7EXAMPLE x", "sk_live_abcdefgh12345678",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "bot 123456789:AAH_abcdefghijklmnopqrstuvwxyz01234", "rk_live_abcdefgh12345678",
    "ghp_" + "a" * 36, "xoxb-123456789012-abcdefghij", "AIza" + "a" * 35,
])
def test_secret_detected(t):
    assert filters.has_secret(t)


def test_no_false_secret():
    assert not filters.has_secret("def total(items):\n    return sum(items)\n")


def test_binary_sniff_and_size():
    assert filters.looks_binary(b"ab\x00cd")
    assert not filters.looks_binary(b"plain text")
    assert filters.MAX_FILE_BYTES == 1_000_000


def syms(chs):
    return {c.symbol for c in chs}


def test_chunk_php():
    lang, chs = chunk.chunk_file("a/Invoice.php", PHP)
    assert lang == "php"
    assert "Invoice" in syms(chs) and "helper_vat_rate" in syms(chs)
    inv = next(c for c in chs if c.symbol == "Invoice")
    # the 2-line `<?php / namespace` header is glue: it rides along with the first definition instead of being a chunk
    assert "function total" in inv.text and "class Invoice" in inv.text and inv.start_line <= 4


def test_chunk_php_large_class_split_into_methods():
    body = "\n".join(f"    public function m{i}() {{\n" + "        $x = 1;\n" * 12 + "    }\n" for i in range(12))
    src = f"<?php\nclass Big\n{{\n{body}}}\n"
    _, chs = chunk.chunk_file("Big.php", src)
    s = syms(chs)
    assert "Big.m0" in s and "Big.m11" in s
    assert all(c.end_line - c.start_line + 1 <= chunk.MAX_CHUNK_LINES for c in chs)


def test_chunk_js():
    _, chs = chunk.chunk_file("a.js", JS)
    assert {"greetUser", "computeTax", "Cart"} <= syms(chs)


def test_chunk_python():
    _, chs = chunk.chunk_file("a.py", PY)
    assert {"pay_order", "Gateway", "decorated_fn"} <= syms(chs)
    g = next(c for c in chs if c.symbol == "Gateway")
    assert "def connect" in g.text


def test_chunk_markdown_by_headings():
    _, chs = chunk.chunk_file("g.md", MD)
    assert {"Guide", "Guide > Setup", "Guide > Usage"} <= syms(chs)


def test_chunk_fallback_windows():
    text = "\n".join(f"line {i}" for i in range(150))
    _, chs = chunk.chunk_file("notes.txt", text)
    assert chs[0].start_line == 1 and chs[0].end_line == 60
    assert chs[1].start_line == 51  # 10 lines overlap
    assert chs[-1].end_line == 150


def test_embed_text_prefix():
    c = chunk.Chunk(1, 2, "f", "body")
    assert chunk.embed_text("src/a.php", c).startswith("src/a.php :: f\n")
