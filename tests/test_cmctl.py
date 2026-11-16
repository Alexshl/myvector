import os

import pytest

from myvec import cmctl, config
from fakecm import FakeCm

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def cm(tmp_path):
    f = FakeCm(tmp_path / "bin")
    p = config.home() / "config.toml"
    config.ensure_home()
    p.write_text(f'cm_path = "{f.path}"\ncm_timeout_s = 3\n')
    cmctl._caps_cache.clear()
    cmctl._ls_cache.clear()
    return f


def test_config_defaults_and_load(cm):
    cfg = config.load_config()
    assert cfg.cm_path == str(cm.path) and cfg.cm_timeout_s == 3.0
    assert "cm_path" in config.default_config_text()
    assert config.Config().cm_path == "~/.local/bin/cm" and config.Config().cm_timeout_s == 90.0


def test_capabilities_ok_and_cached(cm):
    assert cmctl.capabilities() == {"available": True, "send_wait": True, "answer": True, "hint": ""}
    n = len(cm.calls())
    cmctl.capabilities()
    assert len(cm.calls()) == n   # cached by (path, mtime)


def test_capabilities_cache_expires(cm, monkeypatch):
    cmctl.capabilities()
    n = len(cm.calls())
    real = cmctl.time.monotonic
    monkeypatch.setattr(cmctl.time, "monotonic", lambda: real() + cmctl.CAPS_TTL + 1)
    cmctl.capabilities()
    assert len(cm.calls()) > n


def test_capabilities_old_cm(cm):
    cm.set(old=True)
    c = cmctl.capabilities()
    assert c["available"] is False and "cm update" in c["hint"]


def test_capabilities_no_wait_flag(cm):
    cm.set(send_help="usage: cm send <id> <text>")
    assert cmctl.capabilities()["available"] is False


def test_capabilities_missing_binary(tmp_path):
    config.ensure_home()
    (config.home() / "config.toml").write_text(f'cm_path = "{tmp_path}/nope"\n')
    cmctl._caps_cache.clear()
    c = cmctl.capabilities()
    assert c["available"] is False and "nope" in c["hint"]


def test_ls_and_cache(cm):
    cm.set(ls=[{"session_id": SID, "alive": True, "window": "w1", "control": "rc"}])
    ok, m = cmctl.ls(now=100.0)
    assert ok and m == {SID: {"alive": True, "window": "w1", "control": "rc"}}
    n = len(cm.calls())
    cmctl.ls(now=105.0)
    assert len(cm.calls()) == n
    cmctl.ls(now=111.0)
    assert len(cm.calls()) == n + 1


def test_ls_failure_is_unavailable(tmp_path):
    config.ensure_home()
    (config.home() / "config.toml").write_text(f'cm_path = "{tmp_path}/nope"\n')
    cmctl._caps_cache.clear()
    cmctl._ls_cache.clear()
    assert cmctl.ls() == (False, {})


def test_who_parses_and_fails(cm):
    cm.set(who={"session_id": SID, "managed": True, "alive": True, "holders": []})
    assert cmctl.who(SID)["managed"] is True
    cm.set(who={}, who_code=1)
    assert cmctl.who(SID) is None


def test_send_uses_stdin_not_argv(cm):
    code, _, _ = cmctl.send(SID, "MARKER-TEXT", wait=30)
    assert code == 0
    c = cm.calls()[-1]
    assert c["argv"] == ["send", SID, "-", "--wait", "30"] and c["stdin"] == "MARKER-TEXT"
    assert "MARKER-TEXT" not in " ".join(c["argv"])


def test_run_timeout_does_not_hang(cm):
    cm.set(sleep=5)
    code, _, err = cmctl.run(["send", SID, "-"], input="x", timeout=0.5)
    assert code is None and err == "timeout"


def test_stderr_tail_scrubs_text_and_masks(cm):
    cm.set(echo=True, codes={"send": 1})
    code, _, err = cmctl.send(SID, "SECRET-ANSWER password=hunter2hunter2")
    assert code == 1 and "SECRET-ANSWER" not in err and "hunter2" not in err
    assert len(err) <= cmctl.TAIL


def test_expanduser(monkeypatch, tmp_path):
    config.ensure_home()
    (config.home() / "config.toml").write_text('cm_path = "~/x/cm"\n')
    assert cmctl.cm_path() == os.path.expanduser("~/x/cm")
