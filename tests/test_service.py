"""`insthub service`: the unit file it writes.  The real install / start / remove
cycle touches the user's systemd and only runs with INSTHUB_TEST_SYSTEMD=1."""
import os
import shutil
import subprocess
import sys
import time

import pytest

import insthub
from insthub import cli, registry, service


def test_unit_for_a_config_and_for_a_script(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    text = service.unit_text("lab", cfg, python="/opt/venv/bin/python")
    assert f'ExecStart="/opt/venv/bin/python" "-m" "insthub" "serve" "{cfg}"' in text
    assert f"WorkingDirectory={tmp_path}" in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    script = tmp_path / "my server 100%.py"
    script.write_text("")
    text = service.unit_text("gui", script, python="/usr/bin/python3")
    assert f'ExecStart="/usr/bin/python3" "{tmp_path}/my server 100%%.py"' in text      # % is escaped for systemd


def test_bad_targets_and_names(tmp_path):
    with pytest.raises(insthub.BusError, match="does not exist"):
        service.unit_text("x", tmp_path / "missing.toml")
    other = tmp_path / "notes.txt"
    other.write_text("")
    with pytest.raises(insthub.BusError, match="expected a .toml config"):
        service.unit_text("x", other)
    cfg = tmp_path / "a.toml"
    cfg.write_text("")
    with pytest.raises(ValueError, match="bad service name"):
        service.unit_text("has space", cfg)


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd")
def test_systemd_accepts_the_unit(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    unit = tmp_path / "insthub-lab.service"
    unit.write_text(service.unit_text("lab", cfg))
    result = subprocess.run(["systemd-analyze", "verify", "--user", str(unit)], capture_output=True, text=True)
    assert result.returncode == 0 and "insthub-lab.service" not in result.stderr, result.stderr


@pytest.mark.skipif(os.environ.get("INSTHUB_TEST_SYSTEMD") != "1", reason="set INSTHUB_TEST_SYSTEMD=1 to run")
def test_real_service_cycle(tmp_path, monkeypatch, capsys):
    """Install a simulated server as a real user service, find it by name, crash
    it and see systemd bring it back, then remove it."""
    monkeypatch.delenv("INSTHUB_DIR")                    # the service uses the real runtime directory
    name = f"selftest{os.getpid()}"
    cfg = tmp_path / f"{name}.toml"
    cfg.write_text('[devices.selftest_echo]\ndriver = "insthub.sim:Loopback"\n')

    def entry():
        return next((s for s in registry.servers() if s["server"] == name), None)

    def wait_for(condition, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.1)
        return False

    try:
        assert cli.main(["service", "install", str(cfg)]) == 0
        assert "state: active" in capsys.readouterr().out
        assert wait_for(lambda: entry() is not None)
        first_pid = entry()["pid"]
        assert insthub.device("selftest_echo").echo(7) == 7              # found by name from this shell
        assert any(u["name"] == name and u["state"] == "active" and u["enabled"] for u in service.units())
        os.kill(first_pid, 9)                                           # a crash ...
        assert wait_for(lambda: (e := entry()) is not None and e["pid"] != first_pid)   # ... and systemd restarts it
        insthub.close_all()
        assert insthub.device("selftest_echo").echo(8) == 8
    finally:
        insthub.close_all()
        cli.main(["service", "remove", name])
    assert not any(u["name"] == name for u in service.units())
    assert wait_for(lambda: entry() is None)
    assert sys.executable                                               # (keeps the import used)
