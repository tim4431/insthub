"""Extensions: manifests read as data, where insthub looks for them, the folders a server
says its devices come from, and `insthub ext` / `insthub check` (see docs/design.md#extensions)."""
import json
import os
import shlex
import stat
import sys
import textwrap
from pathlib import Path

import pytest

import insthub
from insthub import cli, config, extensions, service, sim
from insthub.config import ConfigWarning

HP = """\
[extension]
id = "hp8562a"
title = "HP 8562A spectrum analyzer"
version = "0.3"
api = 1

[devices.analyzer]
driver = "hpdrv:Analyzer"
doc = "HP 8562A over GPIB"
pad = 18

[devices.analyzer-sim]
driver = "hpdrv:Analyzer"
doc = "a simulated analyzer"
sim = true
exclude = ["write"]

[ui.analyzer]
title = "Spectrum analyzer"
run = ["-m", "hpdrv_ui", "--address", "{address}", "--name", "{device}"]

[ui.quick]
panel = "hpdrv_ui:make"
"""


def extension(folder, manifest=HP, python=True, **files):
    """An extension's folder: its manifest, a virtualenv's python (a stand-in) and other files."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / extensions.MANIFEST).write_text(manifest)
    if python:
        (folder / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (folder / ".venv" / "bin" / "python").write_text("")
    for name, text in files.items():
        (folder / f"{name}.py").write_text(textwrap.dedent(text))
    return folder


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home of its own (your list of extensions), and the places discover() looks in, all empty."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(extensions.ENV)
    monkeypatch.setattr(service, "units", list)
    return tmp_path / "home"


def test_insthub_s_own_extension_is_its_simulated_instruments():
    own = extensions.load(extensions.OWN, "insthub")
    assert own.usable and own.builtin and own.id == "insthub-sim" and own.title == "Simulated instruments"
    assert own.version == insthub.__version__ and own.python == sys.executable
    assert set(own.devices) == {"scope", "synth", "camera", "analyzer", "loopback"}
    for offer in own.devices.values():  # each offer is one of insthub's simulated instruments, which says so
        table = own.device_table(offer)
        assert table == {"driver": offer.driver, "extension": "insthub-sim"}  # installed with insthub: no path
        dev = config.build_device(offer.name, table)
        assert isinstance(dev, sim._Simulated) and dev._rt.describe()["extension"] == "insthub-sim"
    assert extensions.check(extensions.OWN.parent, import_=True) == []
    assert [e.id for e in extensions.discover()] == ["insthub-sim"]  # the tests see nothing else


def test_a_manifest_is_read_as_data(tmp_path):
    folder = extension(tmp_path / "hp", hpdrv="raise RuntimeError('never imported')\n")
    ext = extensions.load(folder / extensions.MANIFEST, "added")
    assert ext.usable and (ext.id, ext.title, ext.version, ext.api) == ("hp8562a", "HP 8562A spectrum analyzer",
                                                                         "0.3", 1)
    assert ext.folder == folder and ext.python == str(folder / ".venv" / "bin" / "python")
    sim_offer = ext.devices["analyzer-sim"]
    assert (sim_offer.driver, sim_offer.doc, sim_offer.keys) == ("hpdrv:Analyzer", "a simulated analyzer",
                                                                 {"sim": True, "exclude": ["write"]})
    assert ext.device_table(sim_offer) == {"driver": "hpdrv:Analyzer", "path": str(folder), "extension": "hp8562a",
                                           "sim": True, "exclude": ["write"]}
    assert ext.ui["analyzer"].run == ("-m", "hpdrv_ui", "--address", "{address}", "--name", "{device}")
    assert ext.ui["quick"] == extensions.UI("quick", "quick", None, "hpdrv_ui:make")
    assert "hpdrv" not in sys.modules
    (folder / ".venv" / "bin" / "python").unlink()  # its own Python, named in the manifest
    (folder / "py").mkdir()
    (folder / "py" / "python3").write_text("")
    (folder / extensions.MANIFEST).write_text(HP.replace('api = 1', 'api = 1\npython = "py/python3"'))
    assert extensions.load(folder / extensions.MANIFEST).python == str(folder / "py" / "python3")


@pytest.mark.parametrize("change, error", [
    (("api = 1", "api = 1\n[devices"), "it is not valid TOML: Expected ']'"),
    (("[extension]", "[extensio]"), "it has no [extension] table"),
    (('id = "hp8562a"\n', ""), "[extension] needs an id"),
    (('id = "hp8562a"', 'id = "hp 8562a"'), "bad [extension] id 'hp 8562a'"),
    (("api = 1", "api = 2"), "written for a newer insthub (api = 2;"),
    (("api = 1", 'api = "1"'), '[extension] api: should be a whole number, not "1"'),
    (('driver = "hpdrv:Analyzer"\ndoc = "HP', 'doc = "HP'), '[devices.analyzer] needs a driver = "module:Class"'),
    (('driver = "hpdrv:Analyzer"\ndoc = "HP', 'driver = 3\ndoc = "HP'),
     '[devices.analyzer] driver: should be text as "module:Class", not 3'),
    (("pad = 18", 'meta = { level = { unti = "V" } }'),
     "[devices.analyzer] meta: level: 'unti' is not one of unit, min, max, step, choices; did you mean 'unit'?"),
    (("pad = 18", 'path = "."'), "[devices.analyzer] path: not for a manifest to say"),
    (("pad = 18", 'extension_version = "0.3"'), "[devices.analyzer] extension_version: not for a manifest"),
    (("sim = true", 'lazy = "yes"'), '[devices.analyzer-sim] lazy: should be true or false, not "yes"'),
    (("[devices.analyzer]", "[devices.'my analyzer']"), "bad [devices.NAME] name 'my analyzer'"),
    (('panel = "hpdrv_ui:make"', ""), "[ui.quick] needs one of run = [...] (an app) and panel ="),
    (('title = "Spectrum analyzer"', 'panel = "x:y"'), "[ui.analyzer] needs one of run"),
    (('run = ["-m"', 'run = [1, "-m"'), "[ui.analyzer] run: should be a list of one or more arguments"),
    (('run = ["-m", "hpdrv_ui", "--address", "{address}", "--name", "{device}"]', "run = []"),
     '[ui.analyzer] run: should be a list of one or more arguments, as ["-m", "mypackage.ui"], not []'),
])
@pytest.mark.filterwarnings("ignore::insthub.config.ConfigWarning")  # (what it does not read: tested below)
def test_what_makes_a_manifest_unusable_is_its_error(tmp_path, change, error):
    folder = extension(tmp_path / "hp", HP.replace(*change))
    ext = extensions.load(folder / extensions.MANIFEST)  # never raises
    assert not ext.usable and error in ext.error, ext.error
    assert all(ui.run or ui.panel for ui in ext.ui.values())  # a UI that names nothing to start is none
    problems = extensions.check(folder)
    assert problems and all(p.startswith(f"{folder / extensions.MANIFEST}: ") for p in problems)
    assert any(error in p for p in problems)


def test_an_extension_needs_a_python(tmp_path):
    folder = extension(tmp_path / "hp", python=False)
    assert "it has no Python: make a virtualenv (.venv) in" in extensions.load(folder / extensions.MANIFEST).error
    (tmp_path / ".venv" / "bin").mkdir(parents=True)  # the folder above has one: a project with sub-projects
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert extensions.load(folder / extensions.MANIFEST).python == str(tmp_path / ".venv" / "bin" / "python")
    (folder / extensions.MANIFEST).write_text(HP.replace("api = 1", 'api = 1\npython = "nope/python"'))
    assert f"[extension] python: {folder / 'nope' / 'python'} does not exist" in \
        extensions.load(folder / extensions.MANIFEST).error


def test_what_this_insthub_does_not_read_is_left_alone_and_warned_about(tmp_path):
    folder = extension(tmp_path / "hp", HP.replace("api = 1", 'api = 1\nhomepage = "x"') + '\n[docs]\nx = 1\n'
                       '[ui.quick2]\npanel = "a:b"\nicon = "scope"\n[widgets.x]\nk = 1\n')
    assert extensions.load(folder / extensions.MANIFEST).usable  # a newer insthub may read them
    with pytest.warns(ConfigWarning) as caught:
        assert extensions.check(folder) == []
    said = [str(w.message) for w in caught]
    assert any("[extension] homepage: insthub" in s and "does not read it" in s for s in said)
    assert any("[docs]: insthub" in s and "nothing reads it yet" in s for s in said)
    assert any("[ui.quick2] icon:" in s for s in said) and any("[widgets]:" in s for s in said)


def test_the_nearest_manifest_is_the_folder_s_or_one_above(tmp_path):
    folder = extension(tmp_path / "hp")
    (folder / "hp_insthub" / "ui").mkdir(parents=True)
    assert extensions.find_manifest(folder / "hp_insthub" / "ui") == folder / extensions.MANIFEST
    assert extensions.find_manifest(folder) == folder / extensions.MANIFEST
    assert extensions.find_manifest(tmp_path) is None


def test_your_list_of_folders(home, tmp_path, capsys):
    hp = extension(tmp_path / "hp")
    (hp / "hp_insthub").mkdir()
    ext = extensions.add_folder(hp / "hp_insthub")  # a folder inside an extension: the extension's
    assert ext.id == "hp8562a" and ext.source == "added"
    listed = home / ".config" / "insthub" / "extensions.toml"
    assert listed.read_text().startswith("# Folders of insthub extensions")
    listed.write_text(listed.read_text() + "# my own note\n")
    extensions.add_folder(hp)  # already listed: nothing changes
    assert extensions.listed_folders() == [str(hp)]
    other = extension(tmp_path / "other", HP.replace("hp8562a", "other"))
    extensions.add_folder(other)
    assert extensions.listed_folders() == [str(hp), str(other)] and "# my own note" in listed.read_text()
    assert [e.id for e in extensions.discover()] == ["insthub-sim", "hp8562a", "other"]
    assert extensions.remove_folder(hp / "hp_insthub") == [str(hp)]  # found from inside it too: what was listed
    assert extensions.listed_folders() == [str(other)]
    with pytest.raises(ValueError, match="is not in"):
        extensions.remove_folder(hp)
    extensions.add_folder(hp / extensions.MANIFEST)  # from its manifest, as add_folder finds it
    assert extensions.remove_folder(hp / extensions.MANIFEST) == [str(hp)]
    with pytest.raises(ValueError, match="no insthub-extension.toml in"):
        extensions.add_folder(tmp_path / "nothing")

    assert cli.main(["ext", "add", str(hp)]) == 0
    assert f"added {hp}: hp8562a (HP 8562A spectrum analyzer)" in capsys.readouterr().out
    assert cli.main(["ext", "list"]) == 0
    out = capsys.readouterr().out
    assert "hp8562a 0.3  HP 8562A spectrum analyzer  (added)" in out and "devices  analyzer, analyzer-sim" in out
    assert "ui       analyzer (app), quick (panel)" in out
    assert cli.main(["ext", "list", "--json"]) == 0
    listed_json = json.loads(capsys.readouterr().out)
    assert listed_json[1]["id"] == "other" and listed_json[2]["folder"] == str(hp)
    assert listed_json[2]["devices"]["analyzer"]["keys"] == {"pad": 18}
    assert cli.main(["ext", "remove", str(hp / "hp_insthub")]) == 0 and extensions.listed_folders() == [str(other)]
    assert capsys.readouterr().out == f"removed {hp}\n"  # what was taken off your list
    assert cli.main(["ext", "remove", str(hp)]) == 1 and "is not in" in capsys.readouterr().err


def test_what_cannot_be_used_is_listed_with_why(home, tmp_path, monkeypatch):
    hp = extension(tmp_path / "hp")
    twin = extension(tmp_path / "twin")  # another copy of the same extension
    broken = extension(tmp_path / "broken", HP.replace("api = 1", "api = 9"))
    empty = tmp_path / "empty"
    empty.mkdir()
    for folder in (hp, twin, broken):
        extensions.add_folder(folder)
    listed = home / ".config" / "insthub" / "extensions.toml"  # and two that add_folder refuses:
    listed.write_text(listed.read_text().replace("]", f', "{empty}", "{tmp_path / "gone"}"]'))
    found = {e.folder: e for e in extensions.discover()}
    assert found[hp].usable and found[twin].hidden_by == str(hp / extensions.MANIFEST)  # the first one wins
    assert "written for a newer insthub" in found[broken].error
    assert found[empty].error == f"no insthub-extension.toml in {empty} or above"
    assert found[tmp_path / "gone"].error == f"no folder {tmp_path / 'gone'}"
    listed.write_text("paths = 1\n")  # a list that cannot be read hides nothing else
    found = extensions.discover()
    assert [(e.id, e.title) for e in found] == [("insthub-sim", "Simulated instruments"), ("", "extensions.toml")]
    assert "paths should be a list of folders" in found[1].error
    monkeypatch.setenv(extensions.ENV, f"{twin}")  # chosen folders replace the rest
    assert [(e.id, e.folder) for e in extensions.discover()][1:] == [("hp8562a", twin)]
    monkeypatch.setenv(extensions.ENV, "none")
    assert len(extensions.discover()) == 1


def test_a_copy_that_cannot_be_used_hides_none_that_can(home, tmp_path, monkeypatch):
    fresh = extension(tmp_path / "fresh", python=False)  # a clone with no .venv yet, in your list (looked in first)
    extensions.add_folder(fresh)
    working = extension(tmp_path / "working")  # the copy that a service runs
    lab = tmp_path / "lab.toml"
    lab.write_text(f'[devices.a]\ndriver = "hpdrv:Analyzer"\npath = "{working}"\n')
    monkeypatch.setattr(service, "units", lambda: [{"name": "lab", "command": f'"python" "-m" "insthub" "serve" "{lab}"'}])
    found = {e.folder: e for e in extensions.discover()}
    assert "it has no Python" in found[fresh].error and found[fresh].hidden_by is None
    assert found[working].usable and found[working].source == "service:lab"
    assert extensions.for_device({"extension": "hp8562a"}).folder == working  # the one that can be used
    monkeypatch.setattr(service, "units", list)  # none can: the first, whose error says why
    assert "it has no Python" in extensions.for_device({"extension": "hp8562a"}).error


def test_another_copy_of_insthub_is_insthub_s_own(home, tmp_path, monkeypatch):
    """A lab's projects install the checkout beside them (-e ../insthub) while the window runs from an
    environment of its own, so the simulated devices of their servers come from another copy of insthub."""
    copy = tmp_path / "checkout" / "insthub"
    copy.mkdir(parents=True)
    (copy / "__init__.py").write_text("")
    (copy / "_version.py").write_text('__version__ = "9.9"\n')
    (copy / extensions.MANIFEST).write_text(extensions.OWN.read_text())
    extension(tmp_path / "checkout", python=True)  # (the checkout's .venv; and a manifest of its own, above)
    ext = extensions.load(copy / extensions.MANIFEST)
    assert ext.usable and ext.builtin and ext.version == "9.9" and ext.python == sys.executable
    assert ext.root == copy.parent and ext.device_table(ext.devices["scope"]) == {"driver": "insthub.sim:SimScope",
                                                                                   "extension": "insthub-sim"}
    assert extensions.check(copy) == []
    monkeypatch.setenv(extensions.ENV, str(copy))
    assert [(e.id, e.manifest) for e in extensions.discover()] == [("insthub-sim", extensions.OWN)]  # listed once
    with insthub.Server("simulators", [sim.Loopback()]) as server:  # as if its devices came from the copy
        entry = server._entry()
    entry["folders"]["loopback"] = [str(copy)]
    found = extensions.for_device({"extension": "insthub-sim"}, entry["folders"]["loopback"])
    assert found.manifest == extensions.OWN and found.source == "insthub"  # this insthub's: its code runs here


def test_a_registry_file_of_another_shape_leads_nowhere(home, tmp_path):
    folders = {"a": [{"path": "/x"}], "b": "/y", "c": ["relative", 3, str(tmp_path)]}  # another version's, say
    (insthub.registry.runtime_dir() / "odd.json").write_text(json.dumps({"server": "odd", "pid": os.getpid(),
                                                                          "folders": folders}))
    assert extensions._server_folders() == [("server:odd", tmp_path, None)]
    (insthub.registry.runtime_dir() / "odd.json").write_text(json.dumps({"server": "odd", "pid": os.getpid(),
                                                                          "folders": ["/x"]}))
    assert [e.id for e in extensions.discover()] == ["insthub-sim"]  # never raises


def test_services_and_running_servers_lead_to_their_extensions(home, tmp_path, monkeypatch):
    svc = extension(tmp_path / "svc", HP.replace("hp8562a", "svc-ext"))
    lab = tmp_path / "servers" / "lab.toml"
    lab.parent.mkdir()
    lab.write_text(f'[devices.a]\ndriver = "x:Y"\npath = "{svc / "drivers"}"\n')
    script = extension(tmp_path / "script", HP.replace("hp8562a", "script-ext"), srv="")
    units = [{"name": "lab", "command": f'"{sys.executable}" "-m" "insthub" "serve" "{lab}"'},
             {"name": "srv", "command": f'"{sys.executable}" "{script / "srv.py"}"'}]  # a .py server: its folder
    monkeypatch.setattr(service, "units", lambda: units)
    run = extension(tmp_path / "run", HP.replace("hp8562a", "run-ext"),
                    rundrv="import insthub\n\nclass Thing(insthub.Device):\n    extension = 'run-ext'\n")
    thing = config.load_object("rundrv:Thing", str(run))
    with insthub.Server("runner", [thing("thing")]):
        found = {e.id: e for e in extensions.discover()}
    assert found["svc-ext"].source == "service:lab" and found["run-ext"].source == "server:runner"
    assert found["script-ext"].source == "service:srv"
    assert found["run-ext"].folder == run


def test_a_server_says_where_its_devices_code_is_on_this_computer_only(tmp_path):
    folder = extension(tmp_path / "ext", HP, drvmod="class Driver:\n    def ping(self):\n        return 1\n",
                       devmod="import insthub\n\nclass Dev(insthub.Device):\n    pass\n")
    driver = config.load_object("drvmod:Driver", str(folder))
    wrapped_dev = type("Sub", (insthub.Wrapped,), {})  # a Wrapped subclass made in this file
    devices = [config.load_object("devmod:Dev", str(folder))("dev"), insthub.wrap(driver, "plain"),
               wrapped_dev(driver, "sub"), sim.Loopback()]
    with insthub.Server("where", devices, tcp=0) as server:
        entry = next(s for s in insthub.discover() if s["server"] == "where")  # in this process
        assert entry["python"] == sys.executable
        assert entry["folders"] == {"dev": [str(folder)], "plain": [str(folder)], "loopback": [str(extensions.OWN.parent)],
                                    "sub": [str(Path(__file__).parent), str(folder)]}  # the subclass's, the driver's
        on_file = json.loads((insthub.registry.runtime_dir() / "where.json").read_text())
        assert on_file["folders"] == entry["folders"] and on_file["python"] == sys.executable
        with insthub.connect(server.addresses[-1]) as c:  # the network hears neither
            assert "folders" not in c.info() and "python" not in c.info()
    found = extensions.for_device({"extension": "hp8562a"}, entry["folders"]["dev"])
    assert found.id == "hp8562a" and found.source == "device" and found.folder == folder


def test_a_device_s_extension(home, tmp_path):
    mine = extension(tmp_path / "mine")
    listed = extension(tmp_path / "listed")
    extensions.add_folder(listed)
    assert extensions.for_device({"extension": "hp8562a"}, [str(mine / "sub")]).folder == mine  # its own copy
    assert extensions.for_device({"extension": "hp8562a"}).folder == listed  # a remote device: the one here
    assert extensions.for_device({"extension": "hp8562a"}, [str(tmp_path)]).folder == listed  # no manifest there
    assert extensions.for_device({"extension": "insthub-sim"}, [str(mine)]).builtin  # another id there
    assert extensions.for_device({"extension": "unknown"}) is None
    assert extensions.for_device({"type": "Box"}) is None


def test_check_an_extension_and_import_its_drivers(tmp_path):
    folder = extension(tmp_path / "chk", HP.replace("api = 1", f'api = 1\npython = "{sys.executable}"'),
                       hpdrv="""\
                       import insthub, sys

                       class Analyzer(insthub.Device):
                           extension = "hp8562a"
                           extension_version = "0.2"
                       """)
    with pytest.warns(ConfigWarning, match=r"says extension_version = \"0.2\", but the manifest's version is "
                                           r"\"0.3\""):
        assert extensions.check(folder, import_=True) == []
    (folder / "PySide6").mkdir()  # a Qt binding, as far as the check can tell
    (folder / "PySide6" / "__init__.py").write_text("")
    (folder / "hpdrv.py").write_text("import PySide6\nimport vendorlib\n")
    with pytest.warns(ConfigWarning, match="driver hpdrv:Analyzer imports PySide6; servers often run without"):
        problems = extensions.check(folder / extensions.MANIFEST, import_=True)
    assert len(problems) == 2 and all(f"cannot import hpdrv:Analyzer with {sys.executable}: ModuleNotFoundError: "
                                      "No module named 'vendorlib'" in p for p in problems)
    assert extensions.check(folder) == []  # without importing: nothing runs, so nothing fails
    assert extensions.check(tmp_path / "nowhere") == [f"{tmp_path / 'nowhere'}: no such file or folder"]
    assert extensions.check(tmp_path) == [f"no insthub-extension.toml in {tmp_path} or above"]


def test_check_import_loads_what_link_errors_names(tmp_path):
    folder = extension(tmp_path / "le", HP.replace("api = 1", f'api = 1\npython = "{sys.executable}"').replace(
        "pad = 18", 'link_errors = ["nosuchmod:Err", "os:sep", "hpdrv:LinkLost"]'), hpdrv="""\
                       import sys
                       sys.stderr.buffer.write(b"the vendor library says \\xff\\xfe\\n")  # (not UTF-8)
                       sys.stderr.flush()

                       class LinkLost(OSError):
                           pass

                       class Analyzer:
                           pass
                       """)
    assert extensions.check(folder) == []  # what they name is known only once imported
    where = f"{folder / extensions.MANIFEST}: [devices.analyzer] link_errors:"
    assert extensions.check(folder, import_=True) == [
        f"{where} cannot load 'nosuchmod:Err' with {sys.executable}: ModuleNotFoundError: No module named 'nosuchmod'",
        f"{where} 'os:sep' is not an exception class"]


def test_check_a_server_file(tmp_path, capsys):
    folder = extension(tmp_path / "hp", HP.replace("api = 1", f'api = 1\npython = "{sys.executable}"'),
                       hpdrv="class Analyzer:\n    def __init__(self, pad=1):\n        pass\n")
    lab = tmp_path / "lab.toml"
    lab.write_text(f'[devices.a]\ndriver = "hpdrv:Analyzer"\npath = "{folder}"\nextension = "hp8562a"\n'
                   f'[devices.b]\ndriver = "hpdrv:Analyzer"\npath = "hp"\nextension = "other"\n')
    mismatch = (f'{lab}: [devices.b] extension = "other", but the manifest of its folder, '
                f'{folder / extensions.MANIFEST}, has id = "hp8562a"')
    assert extensions.check(lab) == [mismatch]
    lab.write_text(f'[devices.a]\ndriver = "hpdrv:Analyzer"\npath = "{folder}"\npda = 2\n')
    problems = extensions.check(lab, import_=True)  # as `insthub service install` checks it
    assert problems == [f"{lab}: [devices.a] pda: not a parameter of hpdrv:Analyzer(pad=1); did you mean 'pad'?"]

    other = extension(tmp_path / "other", HP.replace("hp8562a", "other"))  # with a .venv of its own
    lab.write_text(f'[devices.a]\ndriver = "hpdrv:Analyzer"\npath = "{folder}"\n'
                   f'[devices.b]\ndriver = "hpdrv:Analyzer"\npath = "{other}"\n')
    with pytest.warns(ConfigWarning, match="its devices' extensions run in different Pythons .* host them in "
                                           "separate servers"):
        assert extensions.check(lab) == []
    lab.write_text(f'[devices.a]\ndriver = "hpdrv:Analyzer"\npath = "{folder}"\npda = 2\n')
    assert cli.main(["check", str(lab), "--import"]) == 1
    assert "pda: not a parameter" in capsys.readouterr().out
    assert cli.main(["check", str(folder)]) == 0
    assert f"{folder}: no problems found (--import also imports the drivers)" in capsys.readouterr().out


def test_check_a_server_file_next_to_its_drivers(tmp_path):
    folder = extension(tmp_path / "hp", hpdrv="class Analyzer:\n    pass\n")
    lab = folder / "lab.toml"  # in the extension's folder, where the server finds the module without a path
    lab.write_text('[devices.a]\ndriver = "hpdrv:Analyzer"\nextension = "other"\n'
                   '[devices.b]\ndriver = "insthub.sim:SimScope"\nextension = "insthub-sim"\n')  # (installed: not here)
    mismatch = (f'{lab}: [devices.a] extension = "other", but the manifest of its folder, '
                f'{folder / extensions.MANIFEST}, has id = "hp8562a"')
    assert extensions.check(lab) == [mismatch]
    assert extensions._code_folders(lab) == [folder]  # where discover() looks, from a service that runs it


# ---------------------------------------------------------------- starting a UI (design step 8)
def test_a_ui_is_for_every_device_of_its_extension_or_the_types_it_names(tmp_path):
    scope = extensions.load(extensions.OWN).ui["scope"]  # insthub's own: a panel for its simulated scope only
    assert (scope.title, scope.panel, scope.types) == ("Scope panel", "insthub.sim_ui:scope", ("SimScope",))
    assert scope.fits({"type": "SimScope"}) and not scope.fits({"type": "SimSynth"})
    folder = extension(tmp_path / "hp", HP)
    analyzer = extensions.load(folder / extensions.MANIFEST).ui["analyzer"]
    assert analyzer.types == () and analyzer.fits({"type": "Anything"})  # no types: every device of it
    (folder / extensions.MANIFEST).write_text(HP.replace('panel = "hpdrv_ui:make"', 'panel = "hpdrv_ui:make"\n'
                                                                                    'types = "HP8562A"'))
    assert '[ui.quick] types: should be a list of names' in extensions.load(folder / extensions.MANIFEST).error


def registered(server: str, devices: dict, addresses=("ipc:///nowhere.sock",)):
    """A registry file of a server running here (this process stands in for it): its devices' folders."""
    (insthub.registry.runtime_dir() / f"{server}.json").write_text(json.dumps(
        {"server": server, "pid": os.getpid(), "addresses": list(addresses), "devices": list(devices),
         "folders": devices}))


def test_a_device_s_uis_and_why_it_has_none(home, tmp_path):
    mine = extension(tmp_path / "mine")
    listed = extension(tmp_path / "listed")
    extensions.add_folder(listed)
    analyzer = {"name": "sa", "type": "Analyzer", "extension": "hp8562a"}
    registered("here", {"sa": [str(mine)]})
    ext, uis, why = extensions.device_uis(analyzer, "ipc:///nowhere.sock")  # on this computer: its own copy
    assert ext.folder == mine and [u.name for u in uis] == ["analyzer", "quick"] and why is None
    ext, uis, why = extensions.device_uis(analyzer, "tcp://labpc:8700")  # elsewhere: the copy here
    assert ext.folder == listed and len(uis) == 2 and why is None
    assert extensions.device_uis({**analyzer, "extension": "other"}, "tcp://labpc:8700") == \
        (None, [], "extension other is not installed on this computer: insthub ext add FOLDER")
    assert extensions.device_uis({"name": "box", "type": "Box"}, "tcp://labpc:8700") == (None, [], None)
    for copy in (listed, mine):  # none can be used: the first says why
        (copy / ".venv" / "bin" / "python").unlink()
    ext, uis, why = extensions.device_uis(analyzer, "tcp://labpc:8700")
    assert uis == [] and why.startswith("extension hp8562a cannot be used: it has no Python")
    own = {"name": "scope", "type": "SimScope", "extension": "insthub-sim"}
    assert [u.name for u in extensions.device_uis(own, "tcp://labpc:8700")[1]] == ["scope"]
    assert extensions.device_uis({**own, "type": "SimSynth"}, "tcp://labpc:8700")[1] == []  # the scope's only


APP = """\
import os, sys
print("args", sys.argv[1:])
print("cwd", os.getcwd())
print("path", os.environ["PYTHONPATH"].split(os.pathsep)[0])
sys.exit(int(os.environ.get("APP_EXIT", "0")))
"""


def test_launch_starts_an_app_or_a_panel_with_the_extension_s_python(tmp_path, monkeypatch):
    run = '["app.py", "--address", "{address}", "--name", "{device}", "{other}", "{\\"a\\": 1}"]'
    folder = extension(tmp_path / "hp", HP.replace("api = 1", f'api = 1\npython = "{sys.executable}"').replace(
        'run = ["-m", "hpdrv_ui", "--address", "{address}", "--name", "{device}"]', f"run = {run}"), app=APP)
    ext = extensions.load(folder / extensions.MANIFEST)
    app = extensions.launch(ext, "analyzer", "sa", "tcp://labpc:8700")
    assert app.failure(wait=20) is None and not app.running and app.title == "Spectrum analyzer for sa"
    started, *said = app.log.read_text().splitlines()
    assert app.log == insthub.registry.runtime_dir() / "ui" / "sa" / "analyzer.log"
    assert started.startswith("--- ") and started.endswith(f": {shlex.join(app.process.args)}")  # when, and what ran
    assert said == ["args ['--address', 'tcp://labpc:8700', '--name', 'sa', '{other}', '{\"a\": 1}']",
                    f"cwd {folder}", f"path {folder}"]  # in its folder, which is first on its PYTHONPATH
    monkeypatch.setenv("APP_EXIT", "3")
    assert extensions.launch(ext, "analyzer", "sa", "tcp://labpc:8700").failure(wait=20).endswith(f"path {folder}")

    panel = extensions.launch(ext, "quick", "sa", "tcp://127.0.0.1:9")  # nothing listens there
    assert panel.process.args == [sys.executable, "-m", "insthub", "-a", "tcp://127.0.0.1:9", "panel", "sa",
                                  "hpdrv_ui:make", "--title=quick"]
    assert "error: cannot load the panel 'hpdrv_ui:make': No module named 'hpdrv_ui'" in panel.failure(wait=30)
    for device, address in (("../sa", "tcp://labpc:8700"), ("sa --x", "tcp://labpc:8700"), ("sa", "labpc:8700"),
                            ("sa", "inproc://t")):  # what must not fill a placeholder, or cannot be reached
        with pytest.raises(ValueError):
            extensions.launch(ext, "analyzer", device, address)
    with pytest.raises(ValueError, match="cannot be used"):
        extensions.launch(extensions.load(extension(tmp_path / "x", python=False) / extensions.MANIFEST),
                          "analyzer", "sa", "tcp://labpc:8700")


def test_a_program_started_in_the_background_says_why_it_failed(tmp_path):
    from insthub import inventory

    def started(code, title="job"):
        return inventory.spawn([sys.executable, "-c", code], tmp_path / f"{title}.log", title, cwd=tmp_path)

    failed = started("import sys; print('loading...'); print(); sys.exit('error: no PyQt6 here')")
    assert failed.failure(wait=20) == "loading...\nerror: no PyQt6 here" and failed.cwd == tmp_path
    assert started("pass").failure(wait=20) is None                      # it ended well
    assert started("import sys; sys.exit(4)", "silent").failure(wait=20) == "exit code 4"  # it said nothing
    stopped = started("import sys, time; time.sleep(0.3); sys.exit(2)", "stopped")
    stopped.quiet = True                                                 # stopped on purpose (a restart): no news
    assert stopped.failure(wait=20) is None
    slow = started("import time; time.sleep(30)", "slow")
    assert slow.failure(wait=0.2) is None and slow.running               # it runs
    slow.process.kill()
    slow.process.wait(10)
    assert slow.failure() is None and not slow.running                   # ended by a signal: by whoever sent it


def test_what_a_program_said_is_its_own_not_an_earlier_run_s():
    from insthub import inventory

    log = inventory.ui_log("sa", "quick")                                # a log keeps every run of what writes to it
    first = inventory.spawn([sys.executable, "-c", "import sys; sys.exit('No module named PyQt6')"], log, "a")
    assert first.failure(wait=20) == "No module named PyQt6"
    again = inventory.spawn([sys.executable, "-c", "import sys; sys.exit(3)"], log, "b")
    assert again.failure(wait=20) == "exit code 3"                      # it said nothing: not what the first said
    runs = [line for line in log.read_text().splitlines() if line.startswith("--- ")]
    assert len(runs) == 2 and runs[1].endswith("sys.exit(3)'")           # each run says when it started, and what


def test_the_logs_of_a_device_s_windows_are_in_a_folder_of_its_own():
    from insthub import inventory

    ui = insthub.registry.runtime_dir() / "ui"
    assert inventory.ui_log("a-b", "c") == ui / "a-b" / "c.log" != inventory.ui_log("a", "b-c")
    assert inventory.ui_log("scope") == ui / "scope" / "_view.log" != inventory.ui_log("scope", "view")  # live view
    for device, name in (("../x", None), ("sa", "../x"), ("sa/x", "q"), ("", None), ("sa", "_view")):
        with pytest.raises(ValueError, match="bad (device|UI) name"):  # a device's name came over the network
            inventory.ui_log(device, name)


def test_the_uis_of_a_server_s_devices_are_found_all_at_once(home, tmp_path, monkeypatch):
    listed = extension(tmp_path / "listed")
    extensions.add_folder(listed)
    calls = []
    real = extensions.discover
    monkeypatch.setattr(extensions, "discover", lambda: calls.append(1) or real())
    devices = {f"sa{i}": {"name": f"sa{i}", "type": "Analyzer", "extension": "hp8562a"} for i in range(5)}
    found = extensions.server_uis({**devices, "box": {"type": "Box", "extension": ["a", "list"]}}, "tcp://labpc:8700")
    assert len(calls) == 1                                               # a server elsewhere: one discovery for all
    assert all(found[name][0].folder == listed and len(found[name][1]) == 2 for name in devices)
    assert found["box"] == (None, [], None)                              # what came over the network is only compared
    mine = extension(tmp_path / "mine")
    registered("here", {name: [str(mine)] for name in devices})
    calls.clear()
    found = extensions.server_uis(devices, "ipc:///nowhere.sock")         # a server here: its devices' own folders ...
    assert {found[name][0].folder for name in devices} == {mine} and calls == []  # ... and no discovery at all


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="Unix permissions")
def test_an_extension_that_other_users_may_change_runs_nothing(tmp_path, monkeypatch):
    import grp

    folder = extension(tmp_path / "hp", HP.replace("api = 1", f'api = 1\npython = "{sys.executable}"'))
    manifest = folder / extensions.MANIFEST
    assert extensions.load(manifest).usable                              # yours (also with a umask of 002): fine
    manifest.chmod(manifest.stat().st_mode | stat.S_IWOTH)
    ext = extensions.load(manifest)
    assert ext.error.startswith(f"every user may change {manifest} (chmod o-w {manifest}): insthub runs nothing it "
                                "names") and ext.python is None
    with pytest.raises(ValueError, match="cannot be used"):
        extensions.launch(ext, "analyzer", "sa", "tcp://labpc:8700")
    assert any("every user may change" in p for p in extensions.check(folder, import_=True))  # nor imports anything
    manifest.chmod(0o644)
    folder.chmod(folder.stat().st_mode | stat.S_IWOTH)                   # its folder, as /tmp: anyone may put a
    assert f"every user may change {folder} " in extensions.load(manifest).error  # module, or a manifest, in it
    folder.chmod(folder.stat().st_mode & ~stat.S_IWOTH | stat.S_IWGRP)
    with monkeypatch.context() as m:
        m.setattr(extensions, "_shared_group", lambda gid: "lab")       # a group with others in it
        assert f"the users of group lab may change {folder} (chmod g-w {folder})" in extensions.load(manifest).error
        m.setattr(extensions, "_shared_group", lambda gid: None)        # a group of your own alone (a umask of 002
        assert extensions.load(manifest).usable                          # makes files writable for it): fine
    uid = os.getuid()
    if uid != 0:
        assert extensions._shared_group(0) == grp.getgrgid(0).gr_name    # root's group is not this user's alone
        with monkeypatch.context() as m:
            m.setattr(extensions.os, "getuid", lambda: uid + 1)          # as another user sees it
            assert extensions.load(manifest).error.startswith(f"{manifest} belongs to another user: insthub runs")


def test_insthub_open_opens_the_ui_of_a_device(home, tmp_path, monkeypatch, capsys):
    folder = extension(tmp_path / "hp", HP.replace("api = 1", f'api = 1\npython = "{sys.executable}"'), app=APP,
                       opendrv="import insthub\n\nclass Analyzer(insthub.Device):\n    extension = 'hp8562a'\n")
    (folder / extensions.MANIFEST).write_text((folder / extensions.MANIFEST).read_text().replace(
        'run = ["-m", "hpdrv_ui"', 'run = ["app.py"'))
    analyzer = config.load_object("opendrv:Analyzer", str(folder))
    with insthub.Server("lab", [analyzer("sa"), sim.Loopback()], tcp=0) as server:
        address = server.addresses[0]
        assert cli.main(["-a", address, "open", "sa"]) == 1  # two: it lists them
        err = capsys.readouterr().err
        assert "sa has 2 UIs: name one, as  insthub open sa NAME" in err and "quick            quick (panel)" in err
        assert cli.main(["-a", address, "open", "sa", "analyzer"]) == 0
        assert "opened Spectrum analyzer for sa (pid " in capsys.readouterr().out
        monkeypatch.setenv("APP_EXIT", "1")
        assert cli.main(["-a", address, "open", "sa", "analyzer"]) == 1
        assert f"error: Spectrum analyzer for sa ended with an error:\nargs ['--address', '{address}'" in \
            capsys.readouterr().err
        assert cli.main(["-a", address, "open", "sa", "nope"]) == 1
        assert "error: sa: no UI named 'nope'; it has analyzer, quick" in capsys.readouterr().err
        assert cli.main(["-a", address, "open", "loopback"]) == 1
        assert "error: loopback: its extension, Simulated instruments, has no UI for it (insthub view loopback" in \
            capsys.readouterr().err
