"""`insthub service`: the unit file it writes.  The real install / start / remove
cycle touches the user's systemd and only runs with INSTHUB_TEST_SYSTEMD=1."""
import json
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


def test_install_runs_the_python_the_file_needs(tmp_path, monkeypatch):
    """By default, the one `insthub check --import` checks in (extensions.python_for): here, the
    virtualenv next to the file, not the Python that runs `insthub service install`."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service, "_systemctl", lambda *a, check=True: subprocess.CompletedProcess(a, 0, "", ""))
    checked = []
    monkeypatch.setattr(service, "check_python", lambda target, python: checked.append(python) or [])
    (tmp_path / "rig" / ".venv" / "bin").mkdir(parents=True)
    python = tmp_path / "rig" / ".venv" / "bin" / "python"
    python.write_text("")
    cfg = tmp_path / "rig" / "lab.toml"
    cfg.write_text("[server]\n")
    unit = service.install(cfg, start=False)
    assert checked == [str(python)] and f'ExecStart="{python}" "-m" "insthub" "serve"' in unit.read_text()
    service.install(cfg, start=False, python=sys.executable)              # --python: that one
    assert checked[-1] == sys.executable


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


# ---------------------------------------------------------------- bringing services up to date
def older_unit(name, file, python):
    """The unit an insthub before the limit on restart loops wrote (format 1, which its first line did not say yet):
    restarted 2 s after a crash, forever."""
    return (service.unit_text(name, file, python).replace(" (unit format 2).", ".")
            .replace("StartLimitIntervalSec=300\nStartLimitBurst=5\n", "").replace("RestartSec=5", "RestartSec=2"))


OLD = "RestartSec=2, now 5; without StartLimitIntervalSec=300, StartLimitBurst=5"  #: what is old about older_unit


class Services:
    """systemctl --user for several insthub services: units are written into a folder of their own;
    `show` says what each one's state is, and every call is recorded."""

    def __init__(self, folder, monkeypatch):
        self.folder, self.units = folder / "systemd" / "user", {}
        self.folder.mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(folder))
        monkeypatch.setattr(service, "_systemctl", self)
        monkeypatch.setattr(service, "journal", lambda name, lines=200: "boom")
        self.calls = []
        self.then = {}  #: a call -> what happens meanwhile, once it is made (as if somebody did something)

    def add(self, name, text, state="active", enabled=True, pid=0):
        (self.folder / f"insthub-{name}.service").write_text(text)
        self.units[f"insthub-{name}.service"] = {"ActiveState": state, "SubState": "running" if state == "active"
                                                 else "dead", "UnitFileState": "enabled" if enabled else "disabled",
                                                 "MainPID": str(pid)}
        return self.folder / f"insthub-{name}.service"

    def __call__(self, *args, check=True):
        self.calls.append(args)
        self.then.pop(args, lambda: None)()
        shown ="\n".join("".join(f"{k}={v}\n" for k, v in {"Id": unit, **self.units[unit]}.items())
                          for unit in args[2:] if unit in self.units) if args[0] == "show" else ""
        return subprocess.CompletedProcess(args, 0, shown, "")

    def changes(self):
        """What was asked besides looking."""
        return [c for c in self.calls if c[0] != "show"]


def test_a_unit_of_an_older_insthub_is_stale(tmp_path, monkeypatch):
    systemd = Services(tmp_path, monkeypatch)
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    script = tmp_path / "rig.py"
    script.write_text("")
    systemd.add("now", service.unit_text("now", cfg, sys.executable))
    systemd.add("old", older_unit("old", cfg, sys.executable))
    systemd.add("oldpy", older_unit("oldpy", script, sys.executable))
    systemd.add("mine", older_unit("mine", cfg, sys.executable).replace(service.MARK, "# my own"))  # hand-written
    systemd.add("added", older_unit("added", cfg, sys.executable).replace("Restart=on-failure", "Restart=on-failure\n"
                                                                          "Environment=GPIB=1"))  # changed by hand
    systemd.add("args", older_unit("args", script, sys.executable).replace(f'"{script}"', f'"{script}" "--fast"'))
    systemd.add("gone", older_unit("gone", tmp_path / "rig.py", sys.executable))
    systemd.add("text", '[Service]\nExecStart="/usr/bin/python3" "lab.toml"\n')
    script.unlink()                                                    # ("oldpy" and "args" serve it: not judged now)
    judged = {u["name"]: (u["stale"], u["unchecked"]) for u in service.units()}
    assert judged == {"now": (None, None), "old": (OLD, None), "oldpy": (None, f"the file it serves, {script}, is gone"),
                      "mine": (None, "insthub did not write it"),
                      "added": (None, "it was changed by hand (it sets one key twice)"),
                      "args": (None, f"the file it serves, {script}, is gone"),
                      "gone": (None, f"the file it serves, {script}, is gone"), "text": (None, "insthub did not write it")}
    script.write_text("")
    assert service.units("oldpy")[0]["stale"] == OLD                  # a script's service, as a config's
    assert service.units("args")[0]["unchecked"] == ("its command is not the one insthub writes for its file and "
                                                     "Python")
    assert len(systemd.calls) == 3 and systemd.changes() == []        # (looking is one systemctl call)


def test_only_what_an_older_insthub_wrote_is_stale(tmp_path, monkeypatch):
    """A value somebody chose (or that a newer insthub writes) is never "out of date": update would undo it."""
    systemd = Services(tmp_path, monkeypatch)
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    def now(name):
        return service.unit_text(name, cfg, sys.executable)

    systemd.add("tuned", now("tuned").replace("TimeoutStopSec=20", "TimeoutStopSec=90"))  # an instrument slow to close
    systemd.add("seven", older_unit("seven", cfg, sys.executable).replace("RestartSec=2", "RestartSec=7"))
    systemd.add("extra", now("extra").replace("TimeoutStopSec=20", "TimeoutStopSec=20\nNice=5"))
    systemd.add("newer", now("newer").replace("(unit format 2)", "(unit format 3)").replace("RestartSec=5",
                                                                                          "RestartSec=9"))
    systemd.add("before", now("before").replace(" (unit format 2).", "."))  # this format, before units said theirs
    judged = {u["name"]: (u["stale"], u["unchecked"]) for u in service.units()}
    assert judged == {"tuned": (None, "it was changed by hand (TimeoutStopSec=90, insthub writes 20)"),
                      "seven": (None, "it was changed by hand (RestartSec=7, insthub writes 5)"),
                      "extra": (None, "it was changed by hand (Nice=5, which this insthub does not write)"),
                      "newer": (None, "a newer insthub wrote it (unit format 3; this one writes 2)"),
                      "before": (None, None)}
    [record] = service.update(["tuned"], wait=0)
    assert record.plan == [] and systemd.changes() == []
    assert record.text() == ("tuned: its unit was not checked: it was changed by hand (TimeoutStopSec=90, insthub "
                             "writes 20), so it is left as it is")


def test_a_path_with_dollar_and_percent_reads_back_as_it_was(tmp_path, monkeypatch):
    """systemd wants "$$" and "%%" in ExecStart= for "$" and "%": read back, they are single again."""
    systemd = Services(tmp_path, monkeypatch)
    folder = tmp_path / "lab%1"
    folder.mkdir()
    cfg = folder / "lab.toml"
    cfg.write_text("[server]\n")
    python = tmp_path / "env$1%x" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    systemd.add("odd", service.unit_text("odd", cfg, str(python)))
    [unit] = service.units()
    assert service.command_of(unit) == [str(python), "-m", "insthub", "serve", str(cfg)]
    assert service.target(unit) == cfg and (unit["stale"], unit["unchecked"]) == (None, None)
    assert service._cannot_start(unit) is None


def test_update_brings_each_service_up_to_date_by_one_rule(tmp_path, monkeypatch):
    """Stale and running: written anew and restarted.  Stale and stopped: written anew, not started.
    Up to date: left alone.  Whether it starts at login stays as it was."""
    systemd = Services(tmp_path, monkeypatch)
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    run = systemd.add("run", older_unit("run", cfg, sys.executable), pid=4242)
    off = systemd.add("off", older_unit("off", cfg, sys.executable), state="failed", enabled=False)
    now = systemd.add("now", service.unit_text("now", cfg, sys.executable), pid=4343)
    wants = tmp_path / "systemd" / "user" / "default.target.wants"
    wants.mkdir()
    (wants / run.name).symlink_to(run)                                 # how systemd starts it at login
    before = {path: path.read_text() for path in (run, off, now)}
    checked = service.update(check=True)                               # only looking: nothing changes
    assert [(r.name, r.plan, r.done) for r in checked] == [("now", [], []), ("off", ["rewrite"], []),
                                                           ("run", ["rewrite", "restart"], [])]
    assert systemd.changes() == [] and {path: path.read_text() for path in before} == before
    assert checked[2].text() == f"run: its unit is out of date ({OLD})\n    would write its unit anew and restart it"
    assert checked[0].text() == "now: up to date"

    records = {r.name: r for r in service.update(wait=0)}
    assert run.read_text() == service.unit_text("run", cfg, sys.executable) == service.unit_text("run", cfg)
    assert off.read_text() == service.unit_text("off", cfg, sys.executable) and now.read_text() == before[now]
    assert records["run"].done == ["rewrite", "restart"] and records["off"].done == ["rewrite"]
    assert records["now"].plan == records["now"].done == [] and not any(r.error for r in records.values())
    assert systemd.changes() == [("daemon-reload",),                   # off: written anew, never started
                                 ("daemon-reload",), ("reset-failed", "insthub-run.service"),
                                 ("try-restart", "insthub-run.service")]
    assert (wants / run.name).resolve() == run.resolve()              # still started at login; off still not
    assert records["off"].text().endswith("wrote its unit anew; it does not run, so it is not started; its next "
                                          "start uses the new unit")
    assert {u["name"]: u["stale"] for u in service.units()} == {"now": None, "off": None, "run": None}


def test_a_service_whose_code_changed_is_restarted(tmp_path, monkeypatch, capsys):
    systemd = Services(tmp_path, monkeypatch)
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    (tmp_path / "updev.py").write_text("import insthub\n\nclass Dev(insthub.Device):\n    pass\n")
    dev = insthub.config.load_object("updev:Dev", str(tmp_path))
    systemd.add("lab", service.unit_text("lab", cfg, sys.executable), pid=os.getpid())  # its server: the one below
    with insthub.Server("lab", [dev("box")]):
        [record] = service.update(["lab"], wait=1)
        assert (record.outdated, record.plan, systemd.changes()) == ([], [], [])       # up to date: left alone
        st = (tmp_path / "updev.py").stat()
        os.utime(tmp_path / "updev.py", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))   # the driver edited
        [record] = service.update(["lab"], wait=1)
        assert record.outdated == [f"{tmp_path.name}/updev.py changed since it started"]
        assert record.done == ["restart"] and record.pid == os.getpid()
        assert systemd.changes() == [("reset-failed", "insthub-lab.service"), ("try-restart", "insthub-lab.service")]
        assert record.text() == (f"lab: its server runs older code ({tmp_path.name}/updev.py changed since it "
                                 f"started)\n    restarted it (pid {os.getpid()})")
        assert cli.main(["service", "list"]) == 0                      # the list says so too (the restart was not real)
        assert (f"\n                 its server runs older code ({tmp_path.name}/updev.py changed since it started)\n"
                in capsys.readouterr().out)
    assert service.outdated({"pid": os.getpid()}) == [] and service.outdated({"pid": None}) == []


def test_one_service_that_cannot_be_updated_does_not_stop_the_others(tmp_path, monkeypatch):
    systemd = Services(tmp_path, monkeypatch)
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    gone = str(tmp_path / "old-venv" / "bin" / "python")               # a virtualenv deleted since
    rf = systemd.add("rf", older_unit("rf", cfg, gone), pid=4242)
    systemd.add("lab", older_unit("lab", cfg, sys.executable), pid=4343)
    before = rf.read_text()
    records = {r.name: r for r in service.update(["rf", "nope", "lab"], wait=0)}
    assert list(records) == ["rf", "nope", "lab"]
    assert records["rf"].stale == OLD and records["rf"].done == [] and rf.read_text() == before  # never another Python
    assert records["rf"].error.startswith(f"its Python, {gone}, is gone, so it could not start again; install it again "
                                          f"(insthub service install {cfg} --name rf --python PATH)")
    assert records["nope"].error == "no service insthub-nope (see `insthub service list`)"
    assert records["lab"].done == ["rewrite", "restart"] and records["lab"].error is None
    systemd.add("lab", older_unit("lab", cfg, sys.executable), pid=4343)  # a restart that does not come up
    [record] = service.update(["lab"], wait=0.3)
    assert record.done == ["rewrite"] and record.error == ("insthub-lab has announced no insthub server after 0.3 s. "
                                                           "The end of its log (insthub service logs lab):\nboom")
    # what it advises keeps the service as it is: stopped (and at login or not), running but not at login
    systemd.add("rf", older_unit("rf", cfg, gone), state="inactive", enabled=True)
    systemd.add("by", older_unit("by", cfg, gone), state="failed", enabled=False)
    systemd.add("now", older_unit("now", cfg, gone), pid=4545, enabled=False)
    advice = {r.name: r.error.split("install it again ")[1] for r in service.update(["rf", "by", "now"], wait=0)}
    assert advice == {"rf": f"(insthub service install {cfg} --name rf --python PATH --no-start, then insthub service "
                            "enable rf)",
                      "by": f"(insthub service install {cfg} --name by --python PATH --no-start)",
                      "now": f"(insthub service install {cfg} --name now --python PATH --no-login)"}


def test_a_service_stopped_while_another_restarts_is_not_started(tmp_path, monkeypatch):
    """Each service is looked at again just before its turn; and a restart is systemd's try-restart, which never
    starts one that has stopped."""
    systemd = Services(tmp_path, monkeypatch)
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    systemd.add("a", older_unit("a", cfg, sys.executable), pid=4242)
    b = systemd.add("b", older_unit("b", cfg, sys.executable), pid=4343)
    stopped = {"ActiveState": "inactive", "SubState": "dead", "MainPID": "0"}
    systemd.then[("try-restart", "insthub-a.service")] = lambda: systemd.units[b.name].update(stopped)  # by the user
    records = {r.name: r for r in service.update(wait=0)}
    assert records["a"].done == ["rewrite", "restart"]
    assert records["b"].done == ["rewrite"] and records["b"].state == "inactive" and records["b"].error is None
    assert not any(call[1:] == (b.name,) for call in systemd.changes())   # nothing asked of b: not started
    assert records["b"].outcome.endswith("it does not run, so it is not started; its next start uses the new unit")


def test_service_update_on_the_command_line(tmp_path, monkeypatch, capsys):
    systemd = Services(tmp_path, monkeypatch)
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[server]\n")
    unit = systemd.add("lab", older_unit("lab", cfg, sys.executable), pid=4242)
    systemd.add("rf", service.unit_text("rf", cfg, sys.executable), state="inactive")
    assert cli.main(["service", "list"]) == 0                          # the list says what is old ...
    out = capsys.readouterr().out
    assert f"\n                 its unit is out of date ({OLD})\n" in out
    assert out.endswith("insthub service update brings them up to date (--check: what it would do)\n")
    assert cli.main(["service", "update", "--check"]) == 1             # ... --check, what would change: exit code 1
    assert capsys.readouterr().out == (f"lab: its unit is out of date ({OLD})\n    would write its unit anew and "
                                       "restart it\nrf: up to date\n")
    assert cli.main(["service", "update", "rf", "--check"]) == 0       # nothing to do there
    capsys.readouterr()
    systemd.add("hand", older_unit("hand", cfg, sys.executable).replace(service.MARK, "# mine"), state="inactive")
    assert cli.main(["service", "update", "hand", "--check"]) == 0     # not judged: nothing would change, and it says so
    assert capsys.readouterr().out == ("hand: its unit was not checked: insthub did not write it, so it is left as "
                                       "it is\n")
    (systemd.folder / "insthub-hand.service").unlink()
    del systemd.units["insthub-hand.service"]
    assert cli.main(["service", "update", "--check", "--json"]) == 1
    capsys.readouterr()
    assert systemd.changes() == []
    monkeypatch.setattr(service, "WAIT", 0)
    assert cli.main(["service", "update", "--json"]) == 0
    lab, rf = json.loads(capsys.readouterr().out)
    assert lab == {"name": "lab", "state": "active", "stale": OLD, "unchecked": None, "outdated": [],
                   "plan": ["rewrite", "restart"], "done": ["rewrite", "restart"], "pid": None, "error": None,
                   "check": False}
    assert rf["plan"] == [] and unit.read_text() == service.unit_text("lab", cfg, sys.executable)
    assert cli.main(["service", "update", "--check"]) == 0             # up to date now
    assert cli.main(["service", "update", "nope"]) == 1                # a service that failed: exit code 1
    assert "nope: error: no service insthub-nope" in capsys.readouterr().out
    assert cli.main(["service", "list"]) == 0 and "insthub service update" not in capsys.readouterr().out
