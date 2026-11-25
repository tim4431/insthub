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


def test_units_come_from_one_systemctl_call(tmp_path, monkeypatch):
    folder = tmp_path / "systemd" / "user"
    folder.mkdir(parents=True)
    for name in ("lab", "rf"):
        (folder / f"insthub-{name}.service").write_text(f'[Service]\nExecStart="/usr/bin/python3" "{name}.py"\n')
    calls = []

    def systemctl(*args, check=True):
        calls.append(args)
        out = ("Id=insthub-lab.service\nActiveState=active\nSubState=running\nUnitFileState=enabled\nMainPID=4242\n"
               "\nId=insthub-rf.service\nActiveState=activating\nSubState=auto-restart\nUnitFileState=disabled\n"
               "MainPID=0\n")
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_systemctl", systemctl)
    lab, rf = service.units()
    assert len(calls) == 1 and calls[0][0] == "show"
    assert (lab["name"], lab["state"], lab["sub"], lab["enabled"], lab["pid"]) == ("lab", "active", "running", True, 4242)
    assert (rf["state"], rf["sub"], rf["enabled"], rf["pid"]) == ("activating", "auto-restart", False, None)
    assert lab["command"] == '"/usr/bin/python3" "lab.py"'
    with pytest.raises(insthub.BusError, match="no service insthub-nope"):
        service.restart("nope")
    service.restart("lab")
    assert calls[-1] == ("restart", "insthub-lab.service")


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


def test_tool_environment(tmp_path):
    assert service.tool_environment(str(tmp_path)) is None
    (tmp_path / "uv-receipt.toml").write_text("")
    assert service.tool_environment(str(tmp_path)) == "uv tool"


def test_install_refuses_the_user_wide_command(tmp_path, monkeypatch):
    tool = tmp_path / "tools" / "insthub"
    (tool / "bin").mkdir(parents=True)
    (tool / "uv-receipt.toml").write_text("")
    python = tool / "bin" / "python"
    python.write_text("")
    python.chmod(0o755)
    monkeypatch.setattr(service, "_systemctl", lambda *a, **k: pytest.fail("must not reach systemd"))
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    with pytest.raises(insthub.BusError, match="user-wide `insthub` command"):
        service.install(cfg, python=str(python))  # as the manager window does
    monkeypatch.setattr(sys, "executable", str(python))
    with pytest.raises(insthub.BusError, match="user-wide `insthub` command"):
        service.install(cfg)  # `insthub service install` run from the tool
    with pytest.raises(insthub.BusError, match="no such program"):
        service.install(cfg, python=str(tmp_path / "nope"))


def test_check_python_imports_the_drivers(tmp_path):
    (tmp_path / "gooddrv.py").write_text("class Box:\n    pass\n")
    good = tmp_path / "good.toml"
    good.write_text('[devices.box]\ndriver = "gooddrv:Box"\n')
    service.check_python(good, sys.executable)  # found next to the config, where the service runs
    bad = tmp_path / "bad.toml"
    bad.write_text('[devices.box]\ndriver = "nosuchdriver:Box"\n')
    with pytest.raises(insthub.BusError, match=r"cannot serve bad\.toml: .*nosuchdriver"):
        service.check_python(bad, sys.executable)
    script = tmp_path / "server.py"
    script.write_text("")
    service.check_python(script, sys.executable)
    with pytest.raises(insthub.BusError, match="cannot run"):
        service.check_python(script, str(tmp_path / "missing-python"))


def test_cli_start_stop_restart(monkeypatch, capsys):
    calls = []
    for action in ("start", "stop", "restart"):
        monkeypatch.setattr(service, action, lambda name, a=action: calls.append((a, name)))
    monkeypatch.setattr(service, "units", lambda: [{"name": "lab", "state": "active"}])
    assert cli.main(["service", "restart", "lab"]) == 0
    assert calls == [("restart", "lab")] and "insthub-lab: active" in capsys.readouterr().out
