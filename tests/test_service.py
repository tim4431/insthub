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
from insthub.config import ConfigWarning


def test_unit_for_a_config_and_for_a_script(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    text = service.unit_text("lab", cfg, python="/opt/venv/bin/python")
    assert f'ExecStart="/opt/venv/bin/python" "-m" "insthub" "serve" "{cfg}"' in text
    assert f"WorkingDirectory={tmp_path}" in text
    assert "Restart=on-failure" in text and "WantedBy=default.target" in text
    unit = text.split("[Service]")[0]                                   # a crash loop ends: 5 starts in 5 min
    assert "StartLimitIntervalSec=300" in unit and "StartLimitBurst=5" in unit and "RestartSec=5" in text
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


def test_units_come_from_one_systemctl_call(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "systemd" / "user"
    folder.mkdir(parents=True)
    for name in ("lab", "rf"):
        (folder / f"insthub-{name}.service").write_text(f'[Service]\nExecStart="/usr/bin/python3" "{name}.py"\n')
    calls = []

    def systemctl(*args, check=True):
        calls.append(args)
        out = ("Id=insthub-lab.service\nActiveState=active\nSubState=running\nUnitFileState=enabled\nMainPID=4242\n"
               "NRestarts=0\nResult=success\n"
               "\nId=insthub-rf.service\nActiveState=activating\nSubState=auto-restart\nUnitFileState=disabled\n"
               "MainPID=0\nNRestarts=3\nResult=exit-code\n")
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_systemctl", systemctl)
    lab, rf = service.units()
    assert len(calls) == 1 and calls[0][0] == "show"
    assert (lab["name"], lab["state"], lab["sub"], lab["enabled"], lab["pid"]) == ("lab", "active", "running", True, 4242)
    assert (rf["state"], rf["sub"], rf["enabled"], rf["pid"]) == ("activating", "auto-restart", False, None)
    assert (lab["restarts"], rf["restarts"], rf["result"]) == (0, 3, "exit-code")
    assert service.restarts_text(lab) == "" and service.restarts_text(rf) == "restarted 3 times"
    assert service.restarts_text({"restarts": 1, "result": "start-limit-hit"}) == ("restarted 1 time, gave up: "
                                                                                   "too many restarts")
    assert cli.main(["service", "list"]) == 0
    assert "\n                 restarted 3 times\n" in capsys.readouterr().out
    assert lab["command"] == '"/usr/bin/python3" "lab.py"'
    with pytest.raises(insthub.BusError, match="no service insthub-nope"):
        service.restart("nope")
    service.restart("lab", wait=0)
    assert calls[-2:] == [("reset-failed", "insthub-lab.service"), ("restart", "insthub-lab.service")]


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


def test_check_python_reports_every_problem_and_the_warnings(tmp_path):
    (tmp_path / "PySide2").mkdir()  # a Qt binding, as far as the check can tell
    (tmp_path / "PySide2" / "__init__.py").write_text("")
    (tmp_path / "uidrv.py").write_text("import PySide2\n\nclass Box:\n    def __init__(self, port):\n"
                                       "        pass\n")
    good = tmp_path / "good.toml"
    good.write_text('[devices.box]\ndriver = "uidrv:Box"\nport = 1\n')
    assert service.check_python(good, sys.executable) == [(
        "[devices.box] driver uidrv:Box imports PySide2; servers often run without a screen: move UI code to "
        "the extension's UI module (see docs/design.md#rules-inside-an-extension)")]
    bad = tmp_path / "bad.toml"
    bad.write_text('[devices.box]\ndriver = "uidrv:Box"\nprot = 1\nlazy = "yes"\n')
    with pytest.raises(insthub.BusError) as e:
        service.check_python(bad, sys.executable)
    lines = str(e.value).splitlines()
    assert lines[0] == f'{sys.executable} cannot serve bad.toml: [devices.box] lazy: should be true or false, not "yes"'
    assert lines[1].startswith("[devices.box] prot: not a parameter of uidrv:Box(port)")
    assert lines[-1] == "Fix bad.toml, then install again."  # not: install packages, or use another Python


def test_check_python_is_not_misled_by_what_drivers_print_or_warn(tmp_path):
    (tmp_path / "noisydrv.py").write_text(
        "import sys, warnings\n"
        "sys.stdout.write('loading the driver...')  # no newline\n"
        "warnings.warn('this library is experimental')  # the library's own business, not the file's\n"
        "class Box:\n    def __init__(self, port):\n        pass\n")
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[devices.box]\ndriver = "noisydrv:Box"\nport = 1\n')
    assert service.check_python(cfg, sys.executable) == []
    cfg.write_text('[devices.box]\ndriver = "noisydrv:Box"\n')
    with pytest.raises(insthub.BusError, match=r"cannot serve lab\.toml: \[devices\.box\] noisydrv:Box\(port\): "
                                               r"missing a required argument: 'port'\nFix lab\.toml"):
        service.check_python(cfg, sys.executable)


def test_check_python_says_what_would_fix_it(tmp_path):
    environment = "Install them there, or pass --python"
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[devices.box]\ndriver = "nosuchdriver:Box"\n')
    with pytest.raises(insthub.BusError, match=r"No module named 'nosuchdriver'.*\n.*" + environment):
        service.check_python(cfg, sys.executable)
    for folder in ("one", "two"):  # two folders with a module of one name: no other Python fixes that
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "samedrv.py").write_text("class Box:\n    pass\n")
    cfg.write_text("".join(f'[devices.{f}]\ndriver = "samedrv:Box"\npath = "{tmp_path / f}"\n' for f in ("one", "two")))
    with pytest.raises(insthub.BusError) as e:
        service.check_python(cfg, sys.executable)
    assert "a module named 'samedrv' is loaded already" in str(e.value) and environment not in str(e.value)
    (tmp_path / "brokendrv.py").write_text("class Box(:\n")
    (tmp_path / "numpydrv.py").write_text("raise ValueError('numpy.dtype size changed, may indicate binary "
                                          "incompatibility')\n")
    for driver, error in (("brokendrv", "SyntaxError: "), ("numpydrv", "ValueError: numpy.dtype size changed")):
        cfg.write_text(f'[devices.box]\ndriver = "{driver}:Box"\n')
        with pytest.raises(insthub.BusError, match=rf"cannot serve lab\.toml: \[devices.box\]: cannot load driver "
                                                   rf"'{driver}:Box': {error}") as e:
            service.check_python(cfg, sys.executable)
        assert "\n" not in str(e.value)  # the driver's own error: neither the file (a ValueError) nor the environment


def test_check_python_gives_both_advices_when_both_may_help(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[devices.psu]\ndriver = "mydrvier:PowerSupply"\n\n'
                   '[devices.scope]\ndriver = "insthub.sim:SimScope"\npionts = 10\n')
    with pytest.raises(insthub.BusError) as e:
        service.check_python(cfg, sys.executable)
    lines = str(e.value).splitlines()
    assert lines[0].endswith("cannot serve lab.toml: [devices.psu]: cannot load driver 'mydrvier:PowerSupply': "
                             f"No module named 'mydrvier' (this is {sys.executable}; is it the Python environment "
                             "that has the driver's dependencies?)")
    assert lines[1].startswith("[devices.scope] pionts: not a parameter")
    assert lines[2:] == ["Fix lab.toml, then install again.", service._FIXES["environment"]]


def test_install_shows_the_warnings_and_goes_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_systemctl", lambda *a, check=True: subprocess.CompletedProcess(a, 0, "", ""))
    warning = "[devices.box] driver uidrv:Box imports PyQt6; servers often run without a screen"
    monkeypatch.setattr(service, "check_python", lambda target, python: [warning])
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    with pytest.warns(ConfigWarning, match="imports PyQt6"):
        service.install(cfg, start=False)
    assert cli.main(["service", "install", str(cfg), "--no-start"]) == 0
    out = capsys.readouterr()
    assert f"warning: {warning}\n" in out.err
    assert "installed" in out.out


def test_cli_start_stop_restart(monkeypatch, capsys):
    calls = []
    for action in ("start", "stop", "restart"):
        monkeypatch.setattr(service, action, lambda name, a=action: calls.append((a, name)))
    monkeypatch.setattr(service, "units", lambda: [{"name": "lab", "state": "active"}])
    assert cli.main(["service", "restart", "lab"]) == 0
    assert calls == [("restart", "lab")] and "insthub-lab: active" in capsys.readouterr().out


def test_starting_at_login_is_a_choice(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(service, "WAIT", 0)                             # (no server announces itself here)
    calls = []
    monkeypatch.setattr(service, "_systemctl", lambda *a, check=True: calls.append(a) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(service, "check_python", lambda target, python: [])
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    unit = "insthub-lab.service"
    service.install(cfg, "lab")                                         # now, and at every login
    assert ("enable", unit) in calls and ("start", unit) in calls
    calls.clear()
    service.install(cfg, "lab", login=False)                            # again, without login: that holds now
    assert ("enable", unit) not in calls and ("disable", unit) in calls and ("restart", unit) in calls
    calls.clear()
    service.install(cfg, "rf", start=False)                             # only the file
    assert not any(c[0] in ("enable", "start", "restart") for c in calls)
    service.enable("lab")
    service.disable("lab")
    assert calls[-2:] == [("enable", unit), ("disable", unit)]

    calls.clear()
    monkeypatch.setattr(service, "units", lambda: [{"name": "lab", "state": "active"}])
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["service", "install", str(cfg), "--name", "lab", "--no-login"]) == 0
    out = capsys.readouterr().out
    assert ("enable", unit) not in calls and "does not start at login" in out and "service enable lab" in out
    assert cli.main(["service", "enable", "lab"]) == 0 and "starts at every login" in capsys.readouterr().out
    assert cli.main(["service", "disable", "lab"]) == 0 and "no longer starts at login" in capsys.readouterr().out


class FakeSystemd:
    """systemctl --user for one service, insthub-lab: it records what it is asked, and `show` says
    what `unit` holds."""

    def __init__(self, folder, monkeypatch):
        (folder / "systemd" / "user").mkdir(parents=True)
        (folder / "systemd" / "user" / "insthub-lab.service").write_text('[Service]\nExecStart="python" "lab.toml"\n')
        monkeypatch.setenv("XDG_CONFIG_HOME", str(folder))
        monkeypatch.setattr(service, "_systemctl", self)
        monkeypatch.setattr(service, "journal", lambda name, lines=200: f"the last {lines} lines of {name}\nboom")
        self.calls, self.unit = [], {"ActiveState": "active", "SubState": "running", "MainPID": "0"}

    def __call__(self, *args, check=True):
        self.calls.append(args)
        out = "".join(f"{k}={v}\n" for k, v in {"Id": "insthub-lab.service", **self.unit}.items())
        return subprocess.CompletedProcess(args, 0, out if args[0] == "show" else "", "")


def test_starting_a_service_waits_until_its_server_answers(tmp_path, monkeypatch):
    systemd = FakeSystemd(tmp_path, monkeypatch)
    systemd.unit["MainPID"] = str(os.getpid())                         # its process is this one ...
    with insthub.Server("lab", [insthub.sim.Loopback()]):              # ... and its server has announced itself
        assert service.start("lab") == os.getpid()
        assert systemd.calls[:2] == [("reset-failed", "insthub-lab.service"), ("start", "insthub-lab.service")]
        assert service.restart("lab", wait=1) == os.getpid()           # (one that gave up starts again too)
        assert ("restart", "insthub-lab.service") in systemd.calls


@pytest.mark.parametrize("unit, why", [
    ({"ActiveState": "activating", "SubState": "auto-restart"}, "stopped with an error"),  # crashed, to be restarted
    ({"ActiveState": "failed", "SubState": "failed"}, "stopped with an error"),
    ({"ActiveState": "inactive", "SubState": "dead"}, "ended"),
    ({"ActiveState": "active", "SubState": "running", "MainPID": "4242"},                  # runs, but no server
     "has announced no insthub server after 0.3 s"),
])
def test_a_service_that_does_not_come_up_shows_the_end_of_its_log(tmp_path, monkeypatch, capsys, unit, why):
    systemd = FakeSystemd(tmp_path, monkeypatch)
    systemd.unit.update(unit)
    with pytest.raises(insthub.BusError) as e:
        service.restart("lab", wait=0.3)
    assert str(e.value) == (f"insthub-lab {why}. The end of its log (insthub service logs lab):\n"
                            "the last 20 lines of lab\nboom")
    monkeypatch.setattr(service, "WAIT", 0.3)
    assert cli.main(["service", "start", "lab"]) == 1
    assert f"error: insthub-lab {why}." in capsys.readouterr().err
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    monkeypatch.setattr(service, "check_python", lambda target, python: [])
    with pytest.raises(insthub.BusError, match=rf"^installed .*insthub-lab.service, but insthub-lab {why}"):
        service.install(cfg)                                             # installed all the same: it says so
