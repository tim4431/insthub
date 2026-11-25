"""Devices describe themselves: metadata of properties and command parameters, what is
dangerous, what a device is (extension, simulated), accessor pairs and polling."""
import enum
import time
from typing import Literal

import pytest

import insthub
from insthub import Device, command, config, prop, wrap


class Coupling(enum.Enum):
    AC = "AC"
    DC = "DC"


class Laser(Device):
    """A laser with a shutter."""

    extension = "demo-laser"
    extension_version = "0.2"
    simulated = True
    open_on_start = False

    def open(self):
        self._power, self._mode, self._coupling = 5.0, "CW", "DC"

    @prop(unit="mW", min=0, max=100, step=0.5, dangerous=True)
    def power(self) -> float:
        """Output power."""
        return self._power

    @power.setter
    def power(self, mw):
        self._power = float(mw)

    @prop
    def mode(self) -> Literal["CW", "pulsed"]:
        return self._mode

    @mode.setter
    def mode(self, value):
        self._mode = value

    @prop(choices=["AC"])  # what is declared wins over what the annotation says
    def coupling(self) -> Coupling:
        return self._coupling

    @command(dangerous=True, params={"ms": {"unit": "ms", "min": 0}})
    def fire(self, ms: float = 10.0, coupling: Coupling = Coupling.DC) -> float:
        """Open the shutter for a while."""
        return ms

    @command
    def idn(self) -> str:
        return "laser"


def test_what_a_device_says_about_itself():
    d = Laser("laser")._rt.describe()
    assert (d["extension"], d["extension_version"], d["simulated"]) == ("demo-laser", "0.2", True)
    assert d["properties"]["power"] == {"doc": "Output power.", "settable": True, "dangerous": True,
                                        "meta": {"unit": "mW", "min": 0, "max": 100, "step": 0.5}}
    assert d["properties"]["mode"]["meta"] == {"choices": ["CW", "pulsed"]}  # from the Literal
    assert d["properties"]["coupling"]["meta"] == {"choices": ["AC"]}
    assert d["commands"]["fire"]["dangerous"] is True
    assert d["commands"]["fire"]["params"] == {"ms": {"unit": "ms", "min": 0}, "coupling": {"choices": ["AC", "DC"]}}
    assert d["commands"]["idn"] == {"signature": "() -> str", "doc": "", "concurrent": False}  # nothing to add


def test_a_device_that_says_nothing_describes_itself_as_before():
    class Plain(Device):
        @prop
        def level(self):
            return 1

        @command
        def go(self):
            pass

    d = Plain()._rt.describe()
    assert set(d) == {"name", "type", "doc", "commands", "properties", "streams", "tasks", "status"}
    assert d["properties"]["level"] == {"doc": "", "settable": False} and "params" not in d["commands"]["go"]


def test_a_command_or_property_may_still_have_the_name_of_a_declaration():
    class Old(Device):
        """Written before Device had poll, simulated, extension and dangerous."""

        @command
        def poll(self):
            return "polled"

        @prop
        def simulated(self):
            raise AssertionError("a getter must not run when the tables are made, or in describe()")

        @command
        def dangerous(self):
            return "careful"

    dev = Old()
    d = dev._rt.describe()
    assert {"poll", "dangerous"} <= set(d["commands"]) and "simulated" in d["properties"]
    assert "simulated" not in d and not dev._rt.tasks and not any(c.get("dangerous") for c in d["commands"].values())
    assert dev.poll() == "polled"


def test_the_ways_to_make_a_property_keep_its_metadata():
    def get(self):
        return 1

    def put(self, value):
        pass

    plain = prop(get, put, doc="Level.", unit="V", max=5)
    assert plain.__doc__ == "Level." and plain.meta == {"unit": "V", "max": 5} and plain.fset is put
    marked = prop(unit="V", dangerous=True)(get)
    for copy in (marked.setter(put), marked.getter(get), marked.deleter(put)):
        assert type(copy) is prop and copy.meta == {"unit": "V"} and copy.dangerous is True
    assert marked.setter(put).fget is get and marked.deleter(put).fdel is put
    assert prop(unit=None, min=0).meta == {"min": 0}  # None says nothing
    with pytest.raises(TypeError, match="not callable"):
        plain(get)


def test_metadata_mistakes_are_caught_where_they_are_written():
    with pytest.raises(TypeError, match=r"'units' is not one of unit, min, max, step, choices; did you mean 'unit'"):
        prop(units="V")
    with pytest.raises(TypeError, match=r"min should be a number, not '0'"):
        prop(min="0")
    with pytest.raises(TypeError, match=r"choices should be a list, not 'AC'"):
        command(params={"x": {"choices": "AC"}})(lambda self, x: x)
    assert prop(choices=Coupling).meta == {"choices": ["AC", "DC"]}

    class Typo(Device):
        @command(params={"chanel": {"min": 1}})
        def read(self, channel=1):
            pass

    with pytest.raises(insthub.UnknownCommand, match=r"read\(channel=1\) has no parameter 'chanel'; did you mean "
                                                     r"'channel'"):
        Typo()._rt  # noqa: B018 - making the tables is what checks it


def test_set_meta_changes_what_clients_see():
    class Meter(Laser):
        def open(self):
            super().open()
            self.set_meta("power", max=50, step=None)  # the model connected says so
            self.set_meta("fire.ms", max=1000)

    with insthub.Server("m", [Meter("laser")], tcp=0) as server:
        dev = insthub.connect(server.addresses[1]).laser
        assert dev.bus.describe()["properties"]["power"]["meta"]["max"] == 100  # not opened yet
        dev.idn()
        d = dev.bus.describe()
        assert d["properties"]["power"]["meta"] == {"unit": "mW", "min": 0, "max": 50}
        assert d["commands"]["fire"]["params"]["ms"] == {"unit": "ms", "min": 0, "max": 1000}
        meter = server.devices["laser"]
        meter._rt.build()  # tables made again keep what set_meta said
        assert meter._rt.props["power"].meta["max"] == 50
        meter.set_meta("fire.ms", unit=None, min=None, max=None)
        assert "ms" not in meter._rt.commands["fire"].params
        for name, error in (("powr", "has no property 'powr'; did you mean 'power'"),
                            ("fire.s", r"laser.fire\(ms: .* has no parameter 's'; did you mean 'ms'"), ("shoot.ms", "has no command 'shoot'")):
            with pytest.raises(insthub.UnknownCommand, match=error):
                meter.set_meta(name, unit="x")
        with pytest.raises(TypeError, match="did you mean 'max'"):
            meter.set_meta("power", maxi=3)


def test_dangerous_names_and_everything():
    class Psu(Device):
        dangerous = ("output",)

        @prop
        def voltage(self):
            return 1.0

        @voltage.setter
        def voltage(self, v):
            pass

        @prop
        def current(self):
            return 0.1

        @command
        def output(self, on: bool):
            pass

        @command
        def idn(self):
            return "psu"

    def marked(dev):
        d = dev._rt.describe()
        return sorted(n for kind in ("commands", "properties") for n, e in d[kind].items() if e.get("dangerous"))

    assert marked(Psu()) == ["output"]
    everything = Psu()
    everything.dangerous = True
    assert marked(everything) == ["idn", "output", "voltage"]  # not current: it cannot be written
    more = Psu()  # as a config file or wrap() does: it adds to what the class marks, never takes away
    more.dangerous = ["voltage"]
    assert marked(more) == ["output", "voltage"]
    more.dangerous = ()
    more._rt.build()
    assert marked(more) == ["output"]
    wrong = Psu()
    wrong.dangerous = ["outptu"]
    with pytest.raises(insthub.UnknownCommand, match="dangerous names 'outptu'.*did you mean 'output'"):
        wrong._rt  # noqa: B018


def test_reading_is_never_dangerous():
    class Meter(Device):
        @prop(dangerous=True)  # without a setter: there is nothing to write
        def interlock(self):
            return True

        @prop
        def current(self):
            return 0.1

    assert Meter()._rt.describe()["properties"]["interlock"] == {"doc": "", "settable": False}
    named = Meter()
    named.dangerous = ["current"]
    with pytest.raises(TypeError, match="dangerous names 'current', a property that cannot be written"):
        named._rt  # noqa: B018


def test_choices_come_only_from_annotations_that_list_them():
    class Access(enum.Flag):
        READ = 1
        WRITE = 2

    class Any(Device):
        @command
        def go(self, kind: enum.Enum, access: Access = Access.READ, coupling: Coupling = Coupling.AC):
            pass

    # enum.Enum says what kind of value, not which; a Flag's members combine (READ | WRITE)
    assert Any()._rt.describe()["commands"]["go"]["params"] == {"coupling": {"choices": ["AC", "DC"]}}


class Driver:
    """A driver with accessor pairs, written without insthub in mind."""

    simulated = True

    def __init__(self):
        self._gain, self._offset = 2, 0.0

    def get_gain(self) -> Literal[1, 2, 4]:
        """Amplifier gain."""
        return self._gain

    def set_gain(self, gain):
        self._gain = gain

    def get_offset(self, channel=1):
        return self._offset

    def set_offset(self, volts, channel=1):
        self._offset = volts

    def get_error(self):
        """Read and clear the last error: not a property, nobody may read it by accident."""
        return "none"

    def get_level(self, channel):
        return 0.0

    def set_level(self, level):
        pass

    @property
    def mode(self):
        return "x"

    def get_mode(self):
        return "y"

    def set_mode(self, mode):
        pass


def test_accessor_pairs_become_properties():
    dev = wrap(Driver, "amp", accessors=True, dangerous=["offset"], meta={"offset": {"unit": "V"}})
    d = dev._rt.describe()
    assert sorted(d["properties"]) == ["gain", "mode", "offset"]  # mode: the driver's own property
    assert d["properties"]["gain"] == {"doc": "Amplifier gain.", "settable": True, "meta": {"choices": [1, 2, 4]}}
    assert d["properties"]["offset"] == {"doc": "", "settable": True, "meta": {"unit": "V"}, "dangerous": True}
    assert {"get_gain", "set_gain", "get_error", "get_level"} <= set(d["commands"])  # they stay commands
    assert d["simulated"] is True  # as the driver says
    assert "simulated" not in wrap(Driver, simulated=False)._rt.describe()
    assert sorted(wrap(Driver)._rt.describe()["properties"]) == ["mode"]  # only when asked
    with insthub.Server("a", [dev]) as server:
        amp = server.device("amp")
        amp.gain = 4
        assert amp.gain == 4 and amp.get_gain() == 4
    with pytest.raises(insthub.UnknownCommand, match="dangerous names 'ofset'"):
        wrap(Driver, accessors=True, dangerous=["ofset"])._rt  # noqa: B018


def test_an_accessor_pair_is_dangerous_as_a_whole():
    for marked in ("set_gain", "gain"):  # the property and set_gain write one setting: either marks both
        d = wrap(Driver, accessors=True, dangerous=[marked])._rt.describe()
        assert d["properties"]["gain"].get("dangerous") and d["commands"]["set_gain"].get("dangerous"), marked
        assert not d["commands"]["get_gain"].get("dangerous") and not d["properties"]["offset"].get("dangerous")


class Thermo(Device):
    """Something that is read again and again."""

    def __init__(self, name="thermo"):
        super().__init__(name)
        self.reads = 0
        self.poll = {"temperature": 0.05, "count": 0.05}

    @prop
    def temperature(self) -> float:
        self.reads += 1
        return 20.0 + self.reads

    @command
    def count(self) -> int:
        return self.reads


def test_poll_publishes_what_it_reads():
    with insthub.Server("p", [Thermo()], tcp=0) as server:
        dev = insthub.connect(server.addresses[1]).thermo
        with dev.bus.subscribe("temperature") as temperature, dev.bus.subscribe("count") as count:
            assert temperature.get(2).data > 20 and isinstance(count.get(2).data, int)
        d = dev.bus.describe()
        assert d["tasks"]["poll:temperature"]["interval"] == 0.05 and d["tasks"]["poll:count"]["enabled"]
        assert d["streams"]["temperature"]["doc"] == "temperature, read every 0.05 s (poll)"
        dev.bus.task("poll:temperature", enabled=False)  # a task like any other
        reads = server.devices["thermo"].reads
        time.sleep(0.2)  # about 4 intervals: count still runs, and does not read
        assert server.devices["thermo"].reads - reads <= 1  # (one read may have been running when it stopped)


def test_what_poll_names_must_be_read_without_arguments():
    thermo = Thermo()
    thermo.poll = {"temprature": 1}
    with pytest.raises(insthub.UnknownCommand, match="neither a property nor a command; did you mean 'temperature'"):
        thermo._rt  # noqa: B018
    scope = insthub.sim.SimScope()
    scope.poll = {"set_timebase": 1}
    with pytest.raises(TypeError, match=r"set_timebase\(seconds_per_div: float\) -> float, which needs arguments"):
        scope._rt  # noqa: B018


def test_poll_declared_after_the_runtime_was_made_counts_once_added():
    camera = insthub.sim.SimCamera()  # its constructor has used its runtime (self.tasks) already
    camera.poll = {"exposure": 0.5}  # as a config file does, after the constructor
    assert "poll:exposure" not in camera.tasks
    with insthub.Server("c", [camera]):
        assert camera.tasks["poll:exposure"].interval == 0.5 and camera.tasks["_grab"].interval == 0.05
    camera.poll = None
    camera._rt.build()
    assert "poll:exposure" not in camera.tasks


def test_poll_declared_again_takes_the_new_interval():
    class Camera(Device):
        poll = {"exposure": 1.0}  # noqa: RUF012 - declared on the class, as Device.poll is

        def __init__(self, name="camera"):
            super().__init__(name)
            self.tasks["poll:exposure"]  # the constructor uses the runtime, as SimCamera does

        @prop
        def exposure(self):
            return 0.01

    camera = Camera()
    camera.poll = {"exposure": 10.0}  # as a config file does, after the constructor
    with insthub.Server("c", [camera]):
        assert camera.tasks["poll:exposure"].interval == 10.0
        assert camera._rt.describe()["streams"]["exposure"]["doc"] == "exposure, read every 10 s (poll)"
    task = camera.tasks["poll:exposure"]
    task.interval = 0.5  # changed at run time ($task): kept while the declaration stays
    task.stop()
    camera._rt.build()
    assert (task.interval, task.enabled) == (0.5, False)
    assert camera._rt.describe()["streams"]["exposure"]["doc"] == "exposure, read every 0.5 s (poll)"
    camera.poll = {"exposure": 2.0}
    camera._rt.build()
    assert camera.tasks["poll:exposure"] is task and (task.interval, task.enabled) == (2.0, False)
    # in a config file, meta makes the runtime (with the class's poll) before poll is set, or after it
    for order in (("meta", "poll"), ("poll", "meta")):
        keys = {"meta": {"power": {"min": -40}}, "poll": {"temperature": 5.0}}
        synth = config.build_device("synth", {"driver": "insthub.sim:SimSynth", **{k: keys[k] for k in order}})
        assert synth.tasks["poll:temperature"].interval == 5.0, order
        assert synth._rt.describe()["streams"]["temperature"]["doc"] == "temperature, read every 5 s (poll)"


def test_a_periodic_task_runs_once_at_start_then_after_its_interval():
    thermo = Thermo()
    thermo.poll = {"temperature": 0.5}
    with insthub.Server("t", [thermo]):
        time.sleep(0.25)
        assert thermo.tasks["poll:temperature"].runs == 1


def test_help_and_insthub_info_say_it():
    with insthub.Server("h", [Laser("laser")]) as server:
        lines = server.device("laser").bus.help().splitlines()
    assert lines[0] == "laser: Laser at inproc://h  (simulated, extension demo-laser 0.2)"
    assert "  power  (0 to 100 mW, in steps of 0.5; dangerous to write)" in lines
    assert "  mode  (one of 'CW', 'pulsed')" in lines
    fire = next(i for i, line in enumerate(lines) if line.startswith("  fire("))
    assert lines[fire].endswith(" -> float  (dangerous)")
    assert lines[fire + 1:fire + 4] == ["      Open the shutter for a while.", "      ms: at least 0 ms",
                                        "      coupling: one of 'AC', 'DC'"]
    assert insthub.client.meta_text({"max": 5, "unit": "V", "step": 0.1}) == "at most 5 V, in steps of 0.1"
    assert insthub.client.meta_text({"unit": "s"}) == "s" and insthub.client.meta_text({}) == ""


def test_the_server_announces_it(client):
    assert client.supports("meta") and client.supports("dangerous")
