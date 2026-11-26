"""Config files: checking a device's table against its driver, and loading drivers
from folders without mixing up modules of the same name."""
import inspect
import re
import sys
import tomllib
import warnings
from pathlib import Path

import pytest

import insthub
from insthub import config
from insthub.config import ConfigWarning

DRIVER = """
class PowerSupply:
    def __init__(self, port, baud=9600):
        self.port, self.baud = port, baud
    def voltage(self):
        return 5.0
"""


@pytest.fixture
def imports(monkeypatch, tmp_path):
    """Afterwards, sys.path as it was, and no module of the test's folder in sys.modules."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield
    for name, mod in list(sys.modules.items()):
        if str(tmp_path) in str(getattr(mod, "__file__", None) or getattr(mod, "__path__", "")):
            del sys.modules[name]


def module(folder, name, text=DRIVER):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.py").write_text(text)
    return str(folder)


def table(folder, module_name="psudrv", **keys):
    return {"driver": f"{module_name}:PowerSupply", "path": folder, **keys}


def test_a_misspelled_key_is_caught_when_the_file_is_loaded(tmp_path, imports):
    folder = module(tmp_path, "psudrv")
    with pytest.raises(ValueError) as e:
        config.build_device("psu", table(folder, prot="/dev/ttyUSB0"))
    assert str(e.value).splitlines() == [
        "[devices.psu] prot: not a parameter of psudrv:PowerSupply(port, baud=9600); did you mean 'port'?",
        "[devices.psu] psudrv:PowerSupply(port, baud=9600): missing a required argument: 'port'"]
    dev = config.build_device("psu", table(folder, port="/dev/ttyUSB0", lazy=True))
    assert dev.open_on_start is False and dev._kwargs == {"port": "/dev/ttyUSB0"}


def test_every_problem_of_a_table_in_one_message(tmp_path, imports):
    folder = module(tmp_path, "psudrv")

    def problems(**keys):
        cls, found = config.validate("psu", table(folder, **keys))
        assert cls.__name__ == "PowerSupply"
        return found

    assert problems(lazy="yes", reconnect_interval=-1, exclude="write", link_errors=["VisaIOError"]) == [
        '[devices.psu] lazy: should be true or false, not "yes"',
        "[devices.psu] reconnect_interval: should be a number of seconds, not -1",
        '[devices.psu] exclude: should be a list of names, as ["read", "write"], not "write"',
        '[devices.psu] link_errors: should be a list of "module:Class" names, not ["VisaIOError"]',
        "[devices.psu] psudrv:PowerSupply(port, baud=9600): missing a required argument: 'port'"]
    assert problems(args=["/dev/ttyACM0", 115200]) == []
    assert problems(args=[1, 2, 3]) == [
        "[devices.psu] psudrv:PowerSupply(port, baud=9600): too many positional arguments"]


def test_what_link_errors_names_must_load(tmp_path, imports):
    folder = module(tmp_path, "psudrv")
    assert config.validate("psu", table(folder, port=1, link_errors=["socket:timeout", "nosuchmod:Err"]))[1] == [
        "[devices.psu] link_errors: cannot load 'nosuchmod:Err': No module named 'nosuchmod'"]
    assert config.validate("psu", table(folder, port=1, link_errors=["os:sep"]))[1] == [
        "[devices.psu] link_errors: 'os:sep' is not an exception class"]
    with pytest.raises(ValueError, match="cannot load 'socket:Timeout': module 'socket' has no attribute"):
        config.build_device("psu", table(folder, port=1, link_errors=["socket:Timeout"]))  # before it is made


def test_what_names_no_driver_to_load():
    assert config.validate("x", {"driver": 3, "path": 4, "lazy": "no"}) == (None, [
        '[devices.x] driver: should be text as "module:Class", not 3',
        "[devices.x] path: should be text in quotes, not 4", '[devices.x] lazy: should be true or false, not "no"'])
    assert config.validate("x", {"port": 1}) == (None, ["[devices.x] needs a driver = 'module:Class'"])
    assert config.validate("x", {"driver": "mydriver"}) == (None, [
        '[devices.x] driver: should be text as "module:Class", not "mydriver"'])


def test_what_cannot_be_checked_is_not(tmp_path, imports):
    text = "class Any:\n    def __init__(self, *args, **kwargs):\n        pass\n"
    folder = module(tmp_path, "anydrv", text)
    spec = {"driver": "anydrv:Any", "path": folder}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert config.validate("x", {**spec, "whatever": 1, "args": [1, 2]})[1] == []
        assert config.validate("x", {"driver": "builtins:dict", "anything": 1})[1] == []  # its signature cannot be read
        assert config.validate("x", {**spec, "kwargs": {"lazzy": True}})[1] == []  # said to be the driver's
    # but a key close to one of insthub's own is likely misspelled: insthub would not apply it
    with pytest.warns(ConfigWarning, match=r"^\[devices.x\] lazzy: goes to anydrv:Any\(\*args, \*\*kwargs\), which "
                                           r"takes any keyword; did you mean 'lazy'\? \(to give it to the driver "
                                           r"without this warning, write kwargs = \{ lazzy = \.\.\. \}\)$"):
        dev = config.build_device("x", {**spec, "lazzy": True})
    assert dev._kwargs == {"lazzy": True} and dev.open_on_start  # accepted all the same


def test_an_object_takes_no_arguments(tmp_path, imports):
    folder = module(tmp_path, "objdrv", DRIVER + "\nINSTANCE = PowerSupply(1)\n\ndef factory(port):\n"
                                                "    return PowerSupply(port)\n")
    assert config.validate("x", {"driver": "objdrv:INSTANCE", "path": folder, "prot": 1, "args": [1],
                                 "kwargs": {}})[1] == [
        "[devices.x] prot: objdrv:INSTANCE is an object, not a class, so it takes no arguments",
        "[devices.x] args: objdrv:INSTANCE is an object, not a class, so it takes no arguments"]
    with pytest.raises(ValueError, match=r"port: objdrv:factory is an object, not a class"):
        config.build_device("x", {"driver": "objdrv:factory", "path": folder, "port": 1})
    dev = config.build_device("x", {"driver": "objdrv:INSTANCE", "path": folder})
    assert dev.target is not None and dev.target.port == 1  # hosted as it is


def test_a_constructor_parameter_named_like_a_key_of_insthub(tmp_path, imports):
    folder = module(tmp_path, "lazydrv", "class Psu:\n    def __init__(self, path, lazy=False):\n"
                                         "        self.path = path\n")
    spec = {"driver": "lazydrv:Psu", "path": folder}
    with pytest.raises(ValueError) as e:  # the table gives the key: meant for insthub, or for the driver?
        config.build_device("psu", spec)
    assert str(e.value).splitlines() == [
        ("[devices.psu] path: one of insthub's own keys, but lazydrv:Psu(path, lazy=False) has a parameter 'path' "
         "too, which the key never reaches: say what the driver gets with kwargs = { path = ... }"),
        "[devices.psu] lazydrv:Psu(path, lazy=False): missing a required argument: 'path'"]
    with pytest.raises(ValueError) as e:  # a required one the table does not give: the problem says why
        config.build_device("psu", {"driver": "lazydrv:Psu"})
    assert str(e.value) == ("[devices.psu] lazydrv:Psu(path, lazy=False): missing a required argument: 'path'; "
                            "path is one of insthub's own keys and never reaches it; to pass it, write "
                            "kwargs = { path = ... }")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an optional one is no news at every start of the server
        assert config.build_device("psu", {**spec, "kwargs": {"path": "/dev/ttyS0"}})._kwargs == {"path": "/dev/ttyS0"}
        config.build_device("psu", {**spec, "args": ["/dev/ttyS0"]})
    with pytest.raises(ValueError, match=r"^\[devices.psu\] lazy: one of insthub's own keys, but lazydrv:Psu\(path, "
                                         r"lazy=False\) has a parameter 'lazy' too.*kwargs = \{ lazy = \.\.\. \}$"):
        config.build_device("psu", {**spec, "args": ["/dev/ttyS0"], "lazy": True})  # even when it is optional
    both = config.build_device("psu", {**spec, "args": ["/dev/ttyS0"], "lazy": True, "kwargs": {"lazy": False}})
    assert both.open_on_start is False and both._kwargs == {"lazy": False}  # each as the table says
    cfg = tmp_path / "lab.toml"  # but checking the file (as service install does) mentions it
    cfg.write_text(f'[devices.psu]\ndriver = "lazydrv:Psu"\npath = "{folder}"\nargs = ["/dev/ttyS0"]\n')
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        config.check(cfg)
    assert [str(w.message) for w in caught] == [(
        "[devices.psu] lazydrv:Psu(path, lazy=False) has a parameter 'lazy', but lazy is one of insthub's own keys "
        "and never reaches it; to pass it, write kwargs = { lazy = ... }")]
    with pytest.raises(ValueError, match=r"kwargs\.pth: not a parameter .*; did you mean 'path'\?"):
        config.build_device("psu", {**spec, "kwargs": {"pth": 1}})


def test_options_of_wrapped_drivers_do_nothing_for_a_device():
    spec = {"driver": "insthub.sim:SimScope", "exclude": ["measure"], "points": 10}
    with pytest.warns(ConfigWarning, match=r"exclude: does nothing for insthub.sim:SimScope, a Device subclass"):
        dev = config.build_device("scope", spec)
    assert dev.name == "scope" and dev.points == 10 and "measure" in dev._rt.commands


def test_insthub_keys_set_up_wrapped_drivers_and_devices_alike(tmp_path, imports):
    folder = module(tmp_path, "psudrv")
    keys = {"lazy": True, "reconnect_interval": 3, "link_errors": ["socket:timeout"]}
    wrapped = config.build_device("psu", table(folder, port="x", include=["voltage"], **keys))
    device = config.build_device("scope", {"driver": "insthub.sim:SimScope", **keys})
    for dev in (wrapped, device):
        assert dev.open_on_start is False and dev.reconnect_interval == 3.0
        assert dev.link_errors == (TimeoutError, OSError, EOFError)
    assert list(wrapped._rt.commands) == ["voltage"]


def test_every_option_is_a_reserved_key():
    """A new option of wrap() or of Device must be one of the keys insthub reads itself
    (config._RESERVED), or be listed here with the reason it is not, so that it never
    silently turns into an argument of a driver's constructor."""
    wrap_options = {n for n, p in inspect.signature(insthub.wrap).parameters.items() if p.kind is p.KEYWORD_ONLY}
    assert wrap_options <= set(config._RESERVED)
    device_options = {name for name, value in vars(insthub.Device).items()
                      if not name.startswith("_") and not callable(value) and not isinstance(value, property)}
    not_keys = {"name": "the table's own name", "open_on_start": "set by lazy", "streams": "declared by the class",
                "address": "taken from the constructor's arguments (and many drivers take an `address`)"}
    assert device_options - set(config._RESERVED) == set(not_keys)


def test_check_reports_every_table_and_warns_about_qt(tmp_path, imports, monkeypatch):
    module(tmp_path, "fakeqt_binding", "")
    module(tmp_path, "uidrv", "import fakeqt_binding\n" + DRIVER)
    monkeypatch.setattr(config, "_GUI_MODULES", ("fakeqt_binding",))
    cfg = tmp_path / "lab.toml"
    cfg.write_text(f'[devices.a]\ndriver = "uidrv:PowerSupply"\npath = "{tmp_path}"\nport = 1\n\n'
                   '[devices.b]\ndriver = "insthub.sim:SimScope"\npoints = "many"\nlazzy = true\n')
    with pytest.raises(ValueError) as e, pytest.warns(ConfigWarning, match=r"\[devices.a\] driver uidrv:PowerSupply "
                                                                           r"imports fakeqt_binding; servers"):
        config.check(cfg)
    assert str(e.value) == ("[devices.b] lazzy: not a parameter of insthub.sim:SimScope(name='scope', "
                            "points=1000, latency=0.0); did you mean 'lazy'?")


def test_load_reports_what_check_does_before_making_anything(tmp_path, imports):
    module(tmp_path, "madedrv", "made = []\n\nclass PowerSupply:\n    def __init__(self, port):\n"
                                "        made.append(port)\n")
    cfg = tmp_path / "lab.toml"
    cfg.write_text(f'[server]\nnmae = "lab"\n\n[devices.ok]\ndriver = "madedrv:PowerSupply"\npath = "{tmp_path}"\n'
                   'port = 1\n\n[devices.scope]\ndriver = "insthub.sim:SimScope"\nlazzy = true\n\n'
                   '[devices.psu]\ndriver = "madedrv:PowerSupply"\nprot = 2\n')
    with pytest.raises(ValueError) as loading:
        config.load(cfg)
    with pytest.raises(ValueError) as checking:
        config.check(cfg)
    assert str(loading.value).splitlines() == str(checking.value).splitlines() == [
        "[server] nmae: not one of name, tcp, ipc; did you mean 'name'?",
        ("[devices.scope] lazzy: not a parameter of insthub.sim:SimScope(name='scope', points=1000, latency=0.0); "
         "did you mean 'lazy'?"),
        "[devices.psu] prot: not a parameter of madedrv:PowerSupply(port); did you mean 'port'?",
        "[devices.psu] madedrv:PowerSupply(port): missing a required argument: 'port'"]
    assert sys.modules["madedrv"].made == []  # not even the device that was fine


def test_every_problem_even_with_a_driver_that_cannot_be_loaded(tmp_path, imports):
    cfg = tmp_path / "lab.toml"
    cfg.write_text('[devices.psu]\ndriver = "mydrvier:PowerSupply"\nlazy = "yes"\n\n'
                   '[devices.scope]\ndriver = "insthub.sim:SimScope"\npionts = 10\n')
    for do in (config.check, config.load):
        with pytest.raises(config.ConfigError) as e:
            do(cfg)
        lines = str(e.value).splitlines()
        assert [line.partition(":")[0] for line in lines] == [
            "[devices.psu] lazy", "[devices.psu]", "[devices.scope] pionts"]
        assert lines[1].startswith("[devices.psu]: cannot load driver 'mydrvier:PowerSupply': No module named")
        assert [str(u) for u in e.value.unloadable] == [lines[1]]  # which another Python may have
    with pytest.raises(config.ConfigError, match=r"lazy: .*\n.*cannot load driver"):
        config.build_device("psu", {"driver": "mydrvier:PowerSupply", "lazy": "yes"})
    # when every problem is a driver that cannot be loaded, it may well be the Python environment
    cfg.write_text('[devices.psu]\ndriver = "mydrvier:PowerSupply"\n\n[devices.amp]\ndriver = "nosuchamp:Amp"\n')
    with pytest.raises(ImportError) as e:
        config.check(cfg)
    assert not isinstance(e.value, ValueError) and [line.partition(":")[0] for line in str(e.value).splitlines()] == [
        "[devices.psu]", "[devices.amp]"]


def test_a_drivers_own_error_is_not_one_of_the_file(tmp_path, imports):
    module(tmp_path, "vendordrv", "import insthub\n\nclass Psu(insthub.Device):\n    def __init__(self, port):\n"
                                  "        super().__init__()\n        import nosuchvendorlib\n")
    module(tmp_path, "baddrv", "import insthub\n\nclass Psu(insthub.Device):\n    def __init__(self, port):\n"
                               "        raise ValueError('bad port')\n")
    module(tmp_path, "brokendrv", "raise ValueError('numpy.dtype size changed, may indicate binary "
                                  "incompatibility')\n")
    cfg = tmp_path / "lab.toml"
    for driver, said in (("vendordrv:Psu", "ModuleNotFoundError: No module named 'nosuchvendorlib'"),
                         ("baddrv:Psu", "ValueError: bad port")):
        cfg.write_text(f'[devices.psu]\ndriver = "{driver}"\npath = "{tmp_path}"\nport = 1\n')
        config.check(cfg)  # which constructs no Device subclass
        with pytest.raises(config.DriverError) as e:  # not a ValueError, so not a problem of the file
            config.load(cfg)
        assert str(e.value) == f"[devices.psu]: {driver}(...) failed: {said}"
        assert type(e.value.__cause__).__name__ == said.partition(":")[0]  # with the driver's traceback
    cfg.write_text(f'[devices.box]\ndriver = "brokendrv:Box"\npath = "{tmp_path}"\n')
    with pytest.raises(config.DriverError, match=r"^\[devices.box\]: cannot load driver 'brokendrv:Box': "
                                                 r"ValueError: numpy.dtype size changed"):
        config.check(cfg)


def test_a_relative_path_counts_from_the_files_folder(tmp_path, imports, monkeypatch):
    """Wherever `insthub serve` runs, as a service and the window run it (in the file's folder)."""
    lab = tmp_path / "lab"
    module(lab / "drivers", "reldrv")
    module(tmp_path / "home" / "code", "homedrv")
    for home in ("HOME", "USERPROFILE"):  # (where ~ is, on POSIX and on Windows)
        monkeypatch.setenv(home, str(tmp_path / "home"))
    (lab / "lab.toml").write_text('[devices.a]\ndriver = "reldrv:PowerSupply"\npath = "drivers"\nport = 1\n\n'
                                  '[devices.b]\ndriver = "reldrv:PowerSupply"\npath = "./drivers/../drivers"\n'
                                  'port = 2\n\n'
                                  '[devices.c]\ndriver = "homedrv:PowerSupply"\npath = "~/code"\nport = 3\n\n'
                                  f'[devices.d]\ndriver = "reldrv:PowerSupply"\npath = "{lab / "drivers"}"\n'
                                  'port = 4\n')
    (tmp_path / "elsewhere" / "drivers").mkdir(parents=True)  # (no reldrv here: it would not be found)
    monkeypatch.chdir(tmp_path / "elsewhere")
    config.check("../lab/lab.toml")
    _, devices = config.load("../lab/lab.toml")
    assert [d._kwargs["port"] for d in devices] == [1, 2, 3, 4]
    assert Path(sys.modules["reldrv"].__file__) == (lab / "drivers" / "reldrv.py").resolve()
    assert Path(sys.modules["homedrv"].__file__) == (tmp_path / "home" / "code" / "homedrv.py").resolve()


def test_a_file_that_is_no_toml_is_named(tmp_path):
    cfg = tmp_path / "lab.toml"
    cfg.write_text("[devices.x]\ndriver = \n")
    with pytest.raises(config.ConfigError, match=rf"^{re.escape(str(cfg))}: Invalid value \(at line 2"):
        config.load(cfg)
    with pytest.raises(config.ConfigError, match=rf"^cannot read {re.escape(str(tmp_path))}/missing.toml: No such"):
        config.check(tmp_path / "missing.toml")


def test_a_table_that_is_not_one_is_a_problem_of_the_file(tmp_path, capsys):
    """`devices = 3`, `a = 3` under [devices] or `server = "lab"`: said with the file's other problems, by
    each command that reads the file, not an AttributeError."""
    from insthub import cli, service

    cfg = tmp_path / "lab.toml"
    for text, said in (
            ("devices = 3\n", ["devices: should be a table, as [devices.NAME], not 3"]),
            ('server = "lab"\n\n[devices.echo]\ndriver = "insthub.sim:Loopback"\n',
             ['server: should be a table, as [server], not "lab"']),
            ('[devices]\na = 3\n\n[devices.b]\ndriver = "insthub.sim:Loopback"\nlazy = "yes"\n',
             ['[devices] a: should be a table, as [devices.a] with its driver = "module:Class", not 3',
              '[devices.b] lazy: should be true or false, not "yes"'])):
        cfg.write_text(text)
        for do in (config.check, config.load):
            with pytest.raises(config.ConfigError) as e:
                do(cfg)
            assert str(e.value).splitlines() == said
    assert cli.main(["serve", str(cfg)]) == 1                                   # insthub serve
    assert capsys.readouterr().err.splitlines() == [f"error: {said[0]}", said[1]]
    assert cli.main(["check", str(cfg), "--import"]) == 1                     # insthub check
    assert capsys.readouterr().out.splitlines() == [f"{cfg}: {line}" for line in said]
    report = service.check_report(cfg, sys.executable)                        # what insthub service install asks
    assert report["problem"].splitlines() == said and report["fixes"] == ["file"]


def test_two_folders_with_a_module_of_one_name(tmp_path, imports):
    one, two = module(tmp_path / "one", "clashdrv"), module(tmp_path / "two", "clashdrv")
    config.build_device("a", table(one, "clashdrv", port=1))
    with pytest.raises(ImportError) as e:
        config.build_device("b", table(two, "clashdrv", port=2))
    message = str(e.value)
    assert message.startswith("[devices.b]: cannot load driver 'clashdrv:PowerSupply': a module named 'clashdrv' "
                              f"is loaded already, from {tmp_path / 'one' / 'clashdrv.py'}, so "
                              f"{tmp_path / 'two' / 'clashdrv.py'} cannot be loaded too")
    assert "give one of them a unique package name" in message and "is it the Python environment" not in message
    config.build_device("c", table(one, "clashdrv", port=3))  # the same file again is fine


def test_a_module_found_first_in_another_folder(tmp_path, imports):
    module(tmp_path / "earlier", "shadowdrv")
    later = module(tmp_path / "later", "shadowdrv")
    sys.path.insert(0, str(tmp_path / "earlier"))
    sys.path.insert(1, later)  # on the path already, but behind the other: it would not be used
    with pytest.raises(ImportError, match=r"'shadowdrv' is loaded already, from .*earlier.*so .*later"):
        config.load_object("shadowdrv:PowerSupply", later)


def test_what_is_no_clash(tmp_path, imports):
    for folder in ("one", "two"):  # a namespace package (no __init__.py): Python merges its folders
        module(tmp_path / folder / "nspkg", f"part_{folder}")
        config.load_object(f"nspkg.part_{folder}:PowerSupply", str(tmp_path / folder))
    assert config.load_object("json:loads", str(tmp_path)) is __import__("json").loads  # not in that folder


def test_a_clash_inside_a_namespace_package(tmp_path, imports, monkeypatch):
    """Two checkouts of one extension whose package has no __init__.py: its folders merge, but
    each has a device.py, and Python keeps the first."""
    for folder in ("one", "two"):
        module(tmp_path / folder / "nsext" / "sub", "device")
    monkeypatch.chdir(tmp_path)  # paths as a file next to the folders has them
    config.build_device("a", table("one", "nsext.sub.device", port=1))
    with pytest.raises(ImportError, match=r"a module named 'nsext.sub.device' is loaded already, from "
                                          rf"{re.escape(str(tmp_path / 'one'))}/nsext/sub/device.py, so "
                                          rf"{re.escape(str(tmp_path / 'two'))}/nsext/sub/device.py cannot"):
        config.build_device("b", table("two", "nsext.sub.device", port=2))
    (tmp_path / "three" / "nsext" / "sub").mkdir(parents=True)  # a package where the others have none
    (tmp_path / "three" / "nsext" / "sub" / "__init__.py").write_text("")
    module(tmp_path / "three" / "nsext" / "sub", "device")
    with pytest.raises(ImportError, match=r"a module named 'nsext.sub' is loaded already, from "
                                          r"<module 'nsext.sub' \(namespace\).*, so .*three/nsext/sub/__init__.py cannot"):
        config.build_device("c", table(str(tmp_path / "three"), "nsext.sub.device", port=3))


ACCESSORS = """
class Amp:
    def __init__(self, port):
        self._gain = 1
    def get_gain(self):
        return self._gain
    def set_gain(self, gain):
        self._gain = gain
    def reset(self):
        pass
"""


def test_keys_that_describe_a_device_work_for_wrapped_drivers_and_devices_alike(tmp_path, imports):
    folder = module(tmp_path, "ampdrv", ACCESSORS)
    keys = {"extension": "amp-ext", "extension_version": "0.3", "simulated": True}
    wrapped = config.build_device("amp", {"driver": "ampdrv:Amp", "path": folder, "port": 1, "accessors": True,
                                          "meta": {"gain": {"choices": [1, 2]}}, "poll": {"gain": 2},
                                          "dangerous": ["reset", "gain"], **keys})
    device = config.build_device("synth", {"driver": "insthub.sim:SimSynth", "meta": {"power": {"max": 13}},
                                           "poll": {"frequency": 5}, "dangerous": True, **keys})
    for dev in (wrapped, device):
        d = dev._rt.describe()
        assert (d["extension"], d["extension_version"], d["simulated"]) == ("amp-ext", "0.3", True)
    amp = wrapped._rt.describe()
    assert amp["properties"]["gain"] == {"doc": "", "settable": True, "meta": {"choices": [1, 2]}, "dangerous": True}
    assert amp["commands"]["reset"]["dangerous"] and amp["tasks"]["poll:gain"]["interval"] == 2
    synth = device._rt.describe()
    assert synth["properties"]["power"]["meta"] == {"unit": "dBm", "step": 0.5, "max": 13}
    assert synth["tasks"]["poll:frequency"]["interval"] == 5 and synth["commands"]["rf"]["dangerous"]
    assert "poll:temperature" not in synth["tasks"]  # the file's poll replaces what the class had
    assert not synth["properties"]["locked"].get("dangerous")  # true: what can be written


def test_a_file_adds_dangerous_marks_and_takes_none_away(tmp_path, imports):
    folder = module(tmp_path, "markdrv", "import insthub\n\nclass Psu(insthub.Device):\n"
                                         "    dangerous = ('output',)  # the author's mark\n\n"
                                         "    @insthub.command\n    def output(self, on: bool):\n        pass\n\n"
                                         "    @insthub.command\n    def zero(self):\n        pass\n")
    psu = config.build_device("psu", {"driver": "markdrv:Psu", "path": folder, "dangerous": ["zero"]})
    assert {n for n, c in psu._rt.describe()["commands"].items() if c.get("dangerous")} == {"output", "zero"}


def test_what_is_wrong_with_those_keys(tmp_path, imports):
    folder = module(tmp_path, "ampdrv", ACCESSORS)
    spec = {"driver": "ampdrv:Amp", "path": folder, "port": 1}
    assert config.validate("amp", {**spec, "simulated": "yes", "accessors": 1, "poll": {"gain": -1},
                                   "dangerous": "reset", "meta": {"gain": {"units": "dB"}}})[1] == [
        '[devices.amp] simulated: should be true or false, not "yes"',
        "[devices.amp] accessors: should be true or false, not 1",
        "[devices.amp] poll: should be a table of seconds, as { temperature = 1.0 }, not { gain = -1 }",
        '[devices.amp] dangerous: should be true, or a list of names, as ["fire", "power"], not "reset"',
        "[devices.amp] meta: gain: 'units' is not one of unit, min, max, step, choices; did you mean 'unit'?"]
    unquoted = tomllib.loads("meta = { set_gain.gain = { min = 0 } }")  # TOML: a table "gain" in a table "set_gain"
    assert config.validate("amp", {**spec, **unquoted})[1] == [
        '[devices.amp] meta: set_gain.gain: write a parameter\'s name in quotes, as "set_gain.gain" = { ... }']
    for key, value, said in (("poll", {"gian": 1}, "poll names 'gian', which is neither"),
                             ("dangerous", ["rest"], "dangerous names 'rest'.*did you mean 'reset'"),
                             ("meta", {"gain": {"unit": "dB"}}, "has no property 'gain'")):  # no accessors
        with pytest.raises(ValueError, match=rf"^\[devices.amp\] .*{said}"):
            config.build_device("amp", {**spec, key: value})

    def problems(do, text):
        """The start of each line of what is wrong, by load or by check."""
        cfg = tmp_path / "lab.toml"
        cfg.write_text(text)
        with pytest.raises(ValueError) as e:
            do(cfg)
        return [line.partition(", which")[0].partition("; did")[0] for line in str(e.value).splitlines()]

    amps = (f'[devices.amp]\ndriver = "ampdrv:Amp"\npath = "{folder}"\nport = 1\npoll = {{ gian = 1 }}\n\n'
            f'[devices.amp2]\ndriver = "ampdrv:Amp"\npath = "{folder}"\nport = 2\ndangerous = ["rest"]\n\n')
    devices = ('[devices.synth]\ndriver = "insthub.sim:SimSynth"\npoll = { temprature = 1 }\n\n'
               '[devices.scope]\ndriver = "insthub.sim:SimScope"\nmeta = { "identify.x" = { unit = "V" } }\n')
    # a wrapped driver's device is made without running the driver's code: checked with the file, so
    # that service install catches it, every table of the file at once
    for do in (config.check, config.load):
        assert problems(do, amps + devices) == ["[devices.amp] amp: poll names 'gian'",
                                                "[devices.amp2] amp2: dangerous names 'rest'"]
    # a Device subclass's are known once it is made (its constructor may do anything), which only load
    # does: then all of them at once
    (tmp_path / "lab.toml").write_text(devices)
    config.check(tmp_path / "lab.toml")
    assert problems(config.load, devices) == ["[devices.synth] synth: poll names 'temprature'",
                                              "[devices.scope] scope.identify() -> str has no parameter 'x'"]


def test_a_description_and_where_the_instrument_is(tmp_path, imports):
    module(tmp_path, "psudrv")
    module(tmp_path, "devdrv", "import insthub\nclass Meter(insthub.Device):\n"
                               "    def __init__(self, resource, name=None):\n        super().__init__(name)\n")
    cfg = tmp_path / "lab.toml"
    cfg.write_text(f'[devices.psu]\ndriver = "psudrv:PowerSupply"\npath = "{tmp_path}"\nport = "/dev/ttyUSB0"\n'
                   'description = "the one on the optics table"\n\n'
                   f'[devices.meter]\ndriver = "devdrv:Meter"\npath = "{tmp_path}"\nresource = "GPIB0::22::INSTR"\n')
    _, devices = config.load(cfg)
    psu, meter = (d._rt.describe() for d in devices)
    assert psu["description"] == "the one on the optics table" and psu["address"] == "/dev/ttyUSB0"
    assert meter["address"] == "GPIB0::22::INSTR" and "description" not in meter    # a Device subclass too
    cfg.write_text(f'[devices.psu]\ndriver = "psudrv:PowerSupply"\npath = "{tmp_path}"\nport = 1\ndescription = 5\n')
    with pytest.raises(ValueError, match="description"):
        config.load(cfg)
