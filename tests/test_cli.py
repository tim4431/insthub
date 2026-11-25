import json

import numpy as np
import pytest

import insthub
from insthub import cli, config


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_list_info_call_get_set(server, capsys, tmp_path):
    address = server.addresses[0]
    code, out, _ = run(capsys, "-a", address, "list")
    assert code == 0 and "scope" in out and "SimScope" in out and "connected" in out
    code, out, _ = run(capsys, "-a", address, "info", "synth")
    assert code == 0 and "rf(on: bool = True) -> bool" in out and "frequency" in out
    code, out, err = run(capsys, "-a", address, "call", "scope", "measure", "VPP", "channel=2")
    assert code == 0 and 0.2 < float(out) < 2 and "ms" in err
    assert run(capsys, "-a", address, "set", "synth", "frequency", "3000")[0] == 0
    assert json.loads(run(capsys, "-a", address, "get", "synth", "frequency")[1]) == 3000.0
    code, out, _ = run(capsys, "-a", address, "call", "scope", "get_waveform", "1")
    assert code == 0 and "array float32[1000]" in out and '"channel": 1' in out
    target = tmp_path / "wf.npz"
    assert run(capsys, "-a", address, "call", "scope", "get_waveform", "--save", str(target))[0] == 0
    assert np.load(target)["volts"].shape == (1000,)
    code, _, err = run(capsys, "-a", address, "call", "synth", "sweep", "1", "2")
    assert code == 1 and "error:" in err
    code, _, err = run(capsys, "-a", address, "info", "nope")
    assert code == 1 and "no device 'nope'" in err


def test_release_and_reopen(server, capsys):
    address = server.addresses[0]
    code, out, _ = run(capsys, "-a", address, "release", "scope")
    assert code == 0 and "scope: not connected (released)" in out
    assert server.devices["scope"].connected is False
    code, out, _ = run(capsys, "-a", address, "reopen", "scope")
    assert code == 0 and "scope: connected" in out and server.devices["scope"].connected is True


def test_list_without_servers(capsys):
    assert run(capsys, "list")[0] == 1


def test_serve_from_config(tmp_path):
    (tmp_path / "mydriver.py").write_text(
        "class Box:\n"
        "    def __init__(self, port, gain=1):\n"
        "        self.port, self.gain = port, gain\n"
        "    def read(self):\n"
        "        return [self.port, self.gain]\n"
        "    def close(self):\n"
        "        pass\n")
    cfg = tmp_path / "lab.toml"
    cfg.write_text(f"""
[server]
tcp = 0

[devices.box]
driver = "mydriver:Box"
path = "{tmp_path}"
port = "/dev/ttyACM0"
gain = 3
lazy = true

[devices.scope]
driver = "insthub.sim:SimScope"
points = 500
reconnect_interval = 0.5
""")
    kwargs, devices = config.load(cfg)
    assert kwargs == {"tcp": 0, "name": "lab"} and [d.name for d in devices] == ["box", "scope"]
    assert devices[0].open_on_start is False and devices[1].reconnect_interval == 0.5
    with insthub.Server(kwargs.pop("name"), devices, **kwargs) as server:
        assert server.device("box").read() == ["/dev/ttyACM0", 3]
        assert server.device("scope").get_waveform().volts.shape == (500,)
    with pytest.raises(ValueError, match="needs a driver"):
        config.build_device("x", {})


def test_config_is_written_as_it_is_read(tmp_path):
    devices = {"scope": {"driver": "insthub.sim:SimScope", "points": 500, "lazy": True},
               "odd.name": {"driver": "insthub.sim:Loopback", "args": [], "kwargs": {}},
               "quoted": {"driver": "x:Y", "port": 'C:\\dev "1"\x7f', "gains": [1, 2.5], "opts": {"a b": True}}}
    text = config.dump({"name": "lab", "tcp": "0.0.0.0:8700"}, devices, comment="first line\n\nthird")
    assert text.startswith("# first line\n#\n# third\n\n[server]\n")
    path = tmp_path / "lab.toml"
    path.write_text(text)
    import tomllib

    assert tomllib.loads(text) == {"server": {"name": "lab", "tcp": "0.0.0.0:8700"}, "devices": devices}
    del devices["quoted"]  # no such driver: the others are built
    path.write_text(config.dump({"name": "lab"}, devices))
    kwargs, built = config.load(path)
    assert kwargs == {"name": "lab"} and [d.name for d in built] == ["scope", "odd.name"]
    assert built[0].points == 500 and built[0].open_on_start is False
    with pytest.raises(ValueError, match="TOML has no NoneType"):
        config.dump({"name": "lab"}, {"x": {"driver": "a:B", "port": None}})


def test_parameters_as_typed():
    assert cli.parse_params(["VPP", "2", "channel=1", "on=true", "x=[1, 2]", "a=b=c", "1x=2"]) == \
        (["VPP", 2, "1x=2"], {"channel": 1, "on": True, "x": [1, 2], "a": "b=c"})
    split = cli.split_arguments
    assert split('VPP channel=2  gains=[1, 2] {"a": [3, 4]} \'two words\' note=\'a b\' "q \\" q"') == \
        ["VPP", "channel=2", "gains=[1, 2]", '{"a": [3, 4]}', "two words", "note=a b", '"q \\" q"']
    assert cli.parse_params(split('"text" [1, "two"] x={"k": null}')) == (["text", [1, "two"]], {"x": {"k": None}})
    with pytest.raises(ValueError, match="not closed"):
        split("'open")


def test_gui_without_qt_says_how_to_get_it(capsys, monkeypatch):
    import importlib
    import importlib.util
    import shutil

    from insthub import service

    def no_qt(name, *a, **k):
        raise ImportError("insthub.qt needs a Qt binding: pip install 'insthub[gui]'")

    monkeypatch.setattr(importlib, "import_module", no_qt)
    for kind, fix in (("uv tool", "uv tool install --force --editable"), ("pipx", "pipx inject insthub"),
                      (None, "pip install --python" if shutil.which("uv") and importlib.util.find_spec("pip") is None
                       else "-m pip install pyqtgraph PyQt6")):
        monkeypatch.setattr(service, "tool_environment", lambda prefix=None, kind=kind: kind)
        code, _, err = run(capsys, "gui")
        assert code == 1 and "insthub gui needs Qt" in err and fix in err
    monkeypatch.setattr(service, "tool_environment", lambda prefix=None: "uv tool")
    assert cli.qt_hint("x").endswith('/insthub"')                         # the checkout it runs
