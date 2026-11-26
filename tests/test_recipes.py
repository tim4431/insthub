"""Recipes: Python functions in your recipes folder and in extensions' files, read without running
them, checked against their devices, and run here or in a process of their own (see
docs/design.md#recipes); then `insthub run`, `insthub recipes`, `insthub check FILE.py` and the
agents' run_recipe.  The window's recipes are tested in test_manager.py."""
import json
import os
import signal
import textwrap
import threading
import time

import numpy as np
import pytest

import insthub
from insthub import cli, extensions, mcp, recipes, render
from insthub.recipes import RecipeError

LAB = '''\
"""The comb measurement."""
from typing import Literal

import insthub
from insthub.recipes import recipe

sa = insthub.device("sa")  # the top level: never run to list or check


@recipe(params={"start": {"unit": "MHz", "min": 10}, "label": {"doc": "what to call it"}})
def setup(label: str, start: float = 4e3, dbm: float = 0, mode: Literal["cw", "sweep"] = "cw"):
    """Comb measurement.

    The synth waits at start; the analyzer looks at it."""
    synth = insthub.device("synth")
    synth.frequency = start
    synth.power = dbm
    sa.center = 4e9
    return insthub.device("loopback").echo(f"{label} at {start:g} MHz")


def both(sa=None):
    """Setup, then where the analyzer is."""
    setup("x")
    return center()


def center():
    print("asking the analyzer")
    return insthub.device("sa").center


def _helper():
    insthub.device("camera").snap()
'''


def write(name: str, text: str):
    path = recipes.folder() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def by_name(found):
    return {r.name: r for r in found}


def test_a_recipe_is_read_from_its_file_without_running_it():
    path = write("lab.py", LAB)  # no server runs: importing the file would fail at its top level
    found, problems = recipes.available()
    assert problems == [] and [r.name for r in found] == ["setup", "both", "center"]  # not _helper
    setup = by_name(found)["setup"]
    assert (setup.full_name, setup.file, setup.line, setup.origin) == ("lab.setup", path, 11, recipes.YOURS)
    assert (setup.title, setup.doc) == ("Comb measurement.", "The synth waits at start; the analyzer looks at it.")
    assert [p.text() for p in setup.params.values()] == ["label (required)", "start=4e3 MHz", "dbm=0", 'mode="cw"']
    assert setup.params["start"].default == 4000.0 and setup.params["start"].meta == {"unit": "MHz", "min": 10}
    assert setup.params["mode"].meta == {"choices": ["cw", "sweep"]} and setup.params["label"].doc == "what to call it"
    assert setup.devices == ("synth", "sa", "loopback")
    assert [u.text() for u in setup.uses] == ["synth.frequency = ...", "synth.power = ...", "sa.center = ...",
                                             "loopback.echo()"]
    assert setup.source().startswith("@recipe(params=") and setup.source().endswith('{start:g} MHz")')
    both = by_name(found)["both"]  # what the functions it calls use; its parameter sa is no device
    assert both.devices == ("synth", "sa", "loopback") and both.uses[-1] == recipes.Use("sa", "center", "get", 30)


def test_what_cannot_be_used_leaves_the_rest_usable():
    write("broken.py", "def a(:\n    pass\n")
    write("lab.py", '''\
        from insthub.recipes import recipe

        @recipe(params={"volt": {"units": "V"}})
        def bad(volt=1):
            pass

        async def later():
            pass

        def good(x=[1, 2], when=None, *rest, **more):
            pass
        ''')
    write("_private.py", "def hidden():\n    pass\n")
    found, problems = recipes.available()
    assert len(problems) == 1 and "broken.py:1:" in problems[0] and "none of its recipes can be used" in problems[0]
    found = by_name(found)
    assert set(found) == {"bad", "later", "good"}
    assert "'units' is not one of unit, min, max, step, choices, doc; did you mean 'unit'?" in found["bad"].error
    assert "async def" in found["later"].error
    assert found["good"].error is None and found["good"].takes_more
    with pytest.raises(RecipeError, match=r"recipe lab.bad \(.*lab.py:4\) cannot be used: @recipe params volt"):
        recipes.find("bad")
    problems, notes = recipes.check_file(recipes.folder() / "lab.py")
    assert len(problems) == 2 and any("*rest stays empty" in n for n in notes)


def test_a_recipe_is_found_by_its_name_its_file_s_or_a_path(tmp_path):
    path = write("lab.py", LAB)
    write("synth.py", "def off():\n    pass\n\ndef center():\n    pass\n")
    assert recipes.find("setup").full_name == "lab.setup"
    assert recipes.find("synth.center").file.name == "synth.py"
    with pytest.raises(RecipeError, match=r"several recipes are called 'center': lab.center \(your recipes\), "
                                          r"synth.center \(your recipes\); say which$"):
        recipes.find("center")
    with pytest.raises(RecipeError, match="no recipe 'setpu'; did you mean 'setup'?"):
        recipes.find("setpu")
    other = tmp_path / "elsewhere.py"
    other.write_text(path.read_text())
    recipe = recipes.find(f"{other}:both")
    assert (recipe.origin, recipe.where(), recipe.file) == ("", str(other), other)
    with pytest.raises(RecipeError, match="has no recipe 'bot'; did you mean 'both'?"):
        recipes.find(f"{other}:bot")


def test_an_extension_s_recipes_come_after_yours(tmp_path):
    folder = tmp_path / "experiment"
    (folder / "recipes").mkdir(parents=True)
    (folder / "insthub-extension.toml").write_text('[extension]\nid = "experiment"\nrecipes = ["recipes", "gone.py"]\n')
    (folder / "recipes" / "lab.py").write_text("def setup():\n    pass\n\ndef theirs():\n    pass\n")
    ext = extensions.load(folder / "insthub-extension.toml")
    assert ext.usable and ext.python is None and ext.recipes == (folder / "recipes" / "lab.py",)  # (no Python)
    write("lab.py", LAB)
    found, _ = recipes.available([ext])
    assert [(r.full_name, r.origin) for r in found][-2:] == [("lab.setup", "experiment"), ("lab.theirs", "experiment")]
    assert recipes.find("setup", [ext]).origin == recipes.YOURS  # yours first
    assert recipes.find("theirs", [ext]).where() == "extension experiment"
    assert any("gone.py is neither a .py file nor a folder" in note for note in extensions._read(ext.manifest)[2])


def test_check_asks_every_device_before_anything_runs(server):
    write("lab.py", LAB + '''

def typo():
    insthub.device("synth").frequncy = 1
    insthub.device("sa").span = 1
    insthub.device("nosuch").anything()
    insthub.device(f"s{'ynth'}").rf(False)
    insthub.device("scope").measure("VPP").bus
''')
    checked = recipes.check(recipes.find("setup"))
    assert (checked.problems, checked.dangers, checked.notes) == ([], [], [])
    assert checked.simulated == ["synth", "sa", "loopback"]
    checked = recipes.check(recipes.find("typo"))
    assert checked.problems[0] == "nosuch is not on the bus: start its server"
    assert checked.problems[1].endswith("synth has no property 'frequncy'; did you mean 'frequency'?")
    assert len(checked.problems) == 2  # scope.measure is a command; .bus of its result is not the device's
    assert checked.notes == ["it names a device, command or property only when it runs: that was not checked"]
    insthub.device("synth").bus.call("$pause")
    assert "synth is paused: resume it first" in recipes.check(recipes.find("setup")).problems


class Laser(insthub.Device):
    @insthub.command(dangerous=True)
    def fire(self, ms=10):
        return ms

    @insthub.prop(dangerous=True)
    def power(self):
        return 0.0

    @power.setter
    def power(self, mw):
        pass

    @insthub.prop
    def model(self):
        return "L1"


def test_what_is_dangerous_or_read_only_is_found_before_it_runs():
    write("laser.py", '''\
        import insthub
        from insthub.recipes import recipe

        laser = insthub.device("laser")

        def shoot(ms=10):
            laser.power = 5
            laser.fire.future(ms).result()

        def rename():
            laser.model = "L2"

        @recipe(dangerous=True)
        def careful():
            pass
        ''')
    with insthub.Server("l", [Laser("laser")]):
        assert recipes.check(recipes.find("shoot")).dangers == ["laser: writing its property 'power'",
                                                                "laser: its command 'fire'"]
        assert recipes.check(recipes.find("rename")).problems == ["line 11: laser.model is read-only"]
        assert recipes.check(recipes.find("careful")).dangers == ["the recipe says so (@recipe(dangerous=True))"]


def test_run_calls_the_function_here(server):
    write("lab.py", LAB)
    assert recipes.run("setup", label="here", start=5000) == "here at 5000 MHz"
    assert insthub.device("synth").frequency == 5000
    assert recipes.load("center")() == 4e9
    with pytest.raises(RecipeError, match=r"recipe setup needs label=\.\.\."):
        recipes.run("setup")
    with pytest.raises(RecipeError, match="recipe setup has no parameter 'strat'; did you mean 'start'?"):
        recipes.run("setup", label="x", strat=1)
    with pytest.raises(RecipeError, match="recipe center has no parameter 'x'; it takes none"):
        recipes.run("center", x=1)


def test_start_runs_it_in_a_process_of_its_own(server):
    write("lab.py", LAB + '''

def array(n=3):
    print("making", n)
    return np.arange(n) * 1.5, {"n": n}


def thing():
    return object()

import numpy as np
''')
    run = recipes.start(recipes.find("both"))
    assert run.wait(60) == 4e9 and "asking the analyzer" in run.output() and run.done()
    assert insthub.device("synth").frequency == 4000  # what setup("x") set, from that other process
    values, extra = recipes.start(recipes.find("array"), {"n": 4}).wait(60)
    assert np.array_equal(values, [0, 1.5, 3, 4.5]) and extra == {"n": 4}
    assert recipes.start(recipes.find("thing")).wait(60).startswith("<object object at")  # (as text)
    with pytest.raises(RecipeError, match="needs label="):
        recipes.start(recipes.find("setup"))


def test_a_recipe_that_fails_says_why_from_its_own_code():
    path = write("lab.py", '''\
        def fail(message="boom"):
            print("about to fail")
            raise ValueError(message)
        ''')
    run = recipes.start(recipes.find("fail"), {"message": "no trigger"})
    with pytest.raises(RecipeError, match="recipe fail failed: ValueError: no trigger") as e:
        run.wait(60)
    assert "about to fail" in e.value.output and f'File "{path}", line 3, in fail' in e.value.output
    assert "recipes.py" not in e.value.output  # the traceback starts in the recipe


def test_stop_ends_a_recipe_and_its_finally_blocks_run(tmp_path):
    marker = tmp_path / "cleaned"
    write("slow.py", '''\
        import time
        from pathlib import Path

        def wait(marker):
            try:
                print("waiting", flush=True)
                time.sleep(60)
            finally:
                Path(marker).write_text("cleaned up")
        ''')
    run = recipes.start(recipes.find("wait"), {"marker": str(marker)})
    with pytest.raises(TimeoutError, match="still running after 0.1 s"):
        run.wait(0.1)
    deadline = time.monotonic() + 30
    while "waiting" not in run.output() and time.monotonic() < deadline:
        time.sleep(0.02)
    run.stop()
    with pytest.raises(RecipeError, match="recipe wait was stopped"):
        run.wait(30)
    assert marker.read_text() == "cleaned up" and run.elapsed < 30


def test_save_writes_a_call_as_a_function_and_keeps_the_rest_of_the_file(server):
    path = recipes.save("rf_on", "synth", "rf", (True,))
    assert path == recipes.folder() / "synth.py" and recipes.run("rf_on") is True
    recipes.save("cw", "synth", "set_state", (), {"note": 'a "quoted" value'})  # (any command: here, a made-up one)
    recipes.save("rf_on", "synth", "rf", (False,))
    text = path.read_text()
    assert text.count("def rf_on") == 1 and "insthub.device('synth').rf(False)" in text
    assert text.index("def rf_on") < text.index("def cw") and "note='a \"quoted\" value'" in text
    assert recipes.run("rf_on") is False
    mine = write("mine.py", '"""Mine."""\n# my comment\n\ndef keep():\n    pass\n')
    recipes.save("odd", "mine", "odd-name", (1,))
    assert mine.read_text().startswith('"""Mine."""\nimport insthub\n# my comment\n')
    assert "insthub.device('mine').bus.call('odd-name', 1)" in mine.read_text()
    for bad in ("2x", "class", "_private"):
        with pytest.raises(ValueError, match="cannot be a recipe's name"):
            recipes.save(bad, "synth", "rf")
    write("broken.py", "def a(:\n")
    with pytest.raises(ValueError, match="broken.py is not valid Python"):
        recipes.save("x", "broken", "rf")
    with pytest.raises(ValueError, match="cannot be written in a recipe as Python"):
        recipes.save("x", "synth", "rf", (object(),))
    assert recipes.save_path("lab.synth") == recipes.folder() / "lab_synth.py"


def test_what_a_recipe_calls_in_a_recipes_file_beside_it_counts_as_its_own(server):
    write("redpitaya.py", '''\
        import insthub

        def square(freq=1e3):
            _out().sour_set(1, freq=freq)

        def _out():
            return insthub.device("rp")
        ''')
    comb = write("comb.py", '''\
        import insthub
        import redpitaya
        from redpitaya import square as start_square

        synth, sa = insthub.device("synth"), insthub.device("sa")

        def scan():
            synth.frequency = 1000
            redpitaya.square()
            start_square(2e3)
        ''')
    scan = recipes.find("scan")
    assert scan.devices == ("synth", "rp")  # (_out() names rp; what it returns is not followed, so sour_set is not)
    assert [u.text() for u in scan.uses] == ["synth.frequency = ..."] and scan.at_import == ("synth", "sa")
    write("redpitaya.py", '''\
        import insthub

        rp = insthub.device("rp")

        def square(freq=1e3):
            rp.sour_set(1, freq=freq)
        ''')
    scan = recipes.find("scan")  # comb.py did not change, but the file beside it did
    assert scan.devices == ("synth", "rp") and scan.uses[-1] == recipes.Use("rp", "sour_set", "call", 6,
                                                                            "redpitaya.py")
    assert recipes.check(scan).problems == ["rp is not on the bus: start its server"]
    assert recipes.check_file(comb) == ([], [])
    write("comb.py", comb.read_text().replace('insthub.device("sa")', 'insthub.device("lockin")'))
    assert recipes.check(recipes.find("scan")).problems == [
        "rp is not on the bus: start its server",
        "lockin is not on the bus (the top of comb.py asks for it): start its server"]


def test_a_recipes_file_that_imports_one_that_imports_it_back_is_read():
    write("a.py", "import insthub\nimport b\n\ndef one():\n    insthub.device('synth').rf()\n    b.two()\n")
    write("b.py", "import insthub\nimport a\n\ndef two():\n    insthub.device('sa').peak()\n    a.one()\n")
    assert recipes.find("one").devices == ("synth", "sa") and recipes.find("two").devices == ("sa", "synth")


def test_a_recipes_file_named_as_a_module_is_said_to_hide_it():
    path = write("time.py", "def wait():\n    pass\n")
    assert any("`import time` finds this file instead of the module time" in n for n in recipes.check_file(path)[1])
    assert recipes.find("wait").params == {} and recipes.Param("x", 1, "1").literal
    assert not recipes.Param("x").literal and not recipes.Param("x", recipes._UNREAD, "f()").literal


def test_what_runs_is_read_in_every_way_a_device_is_used():
    write("devices.py", 'import insthub\n\nlaser = insthub.device("laser")\n')
    path = write("lab.py", '''\
        import insthub
        import devices
        from devices import laser as beam

        scope = insthub.device("scope")
        scope.single()                      # at the top: runs whenever any recipe of this file starts
        NAME = "sa"
        sa = insthub.device(NAME)           # named by a variable: not known before it runs

        def shoot(s=1):
            g = insthub.device("gun").with_timeout(s)
            h = g
            h.fire()
            insthub.device("gun").call("reload", 2)
            insthub.device("gun").bus.set("power", 3)
            insthub.device("gun").bus.call(command_name())
            beam.fire()
            devices.laser.power = 5
            a, b = insthub.device("a"), insthub.device("b")
            b.go()

        def command_name():
            return "x"

        if __name__ == "__main__":
            insthub.device("main_only").anything()
        ''')
    shoot = recipes.find("shoot")
    assert shoot.devices == ("gun", "laser", "a", "b") and shoot.at_import == ("scope", "laser")
    assert [(u.text(), u.where()) for u in shoot.uses] == [
        ("scope.single()", "line 6"), ("gun.with_timeout()", "line 11"), ("gun.fire()", "line 13"),
        ("gun.reload()", "line 14"), ("gun.call()", "line 14"), ("gun.power = ...", "line 15"),
        ("laser.fire()", "line 17"), ("laser.power = ...", "line 18"), ("b.go()", "line 20")]
    assert shoot.named_later  # sa = insthub.device(NAME), and bus.call(command_name())
    assert "main_only" not in shoot.devices + shoot.at_import
    assert any("only when it runs" in note for note in recipes.check_file(path)[1])


def test_files_that_import_each_other_are_followed_both_ways():
    write("a.py", "import insthub\nimport b\n\ndef one():\n    b.two()\n\ndef three():\n"
                  "    insthub.device('laser').fire()\n")
    write("b.py", "import insthub\nimport a\n\ndef two():\n    insthub.device('sa').peak()\n    a.three()\n")
    one = recipes.find("one")
    assert one.devices == ("sa", "laser") and [u.text() for u in one.uses] == ["laser.fire()", "sa.peak()"]
    assert [u.file for u in one.uses] == ["", "b.py"]  # (one's own file first)


def test_a_file_beside_it_that_appears_later_is_followed():
    write("lab.py", "import helper\n\ndef go():\n    helper.arm()\n")
    assert recipes.find("go").devices == ()
    write("helper.py", "import insthub\n\ndef arm():\n    insthub.device('rp').arm()\n")
    assert recipes.find("go").devices == ("rp",)  # (lab.py itself did not change)


def test_what_insthub_cannot_read_of_one_recipe_leaves_the_others_usable():
    write("odd.py", '''\
        from typing import Literal
        from insthub.recipes import recipe

        @recipe(params={"freq": {{"unit": "Hz"}}})
        def braces(freq=1):
            pass

        def choices(x: Literal[{"a": [1]}["a"]] = 1):
            pass

        def fine():
            pass
        ''')
    found = by_name(recipes.available()[0])
    assert "write it out as a literal" in found["braces"].error and found["fine"].error is None
    assert found["choices"].params["x"].meta == {}


def test_a_file_with_a_bom_or_a_coding_line_is_read():
    path = recipes.folder() / "bom.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xef\xbb\xbf" + "def café():\n    return 'crème'\n".encode())
    assert recipes.find("café").error is None and recipes.run("café") == "crème"
    recipes.save("other", "bom", "rf")
    assert path.read_bytes().startswith(b"\xef\xbb\xbfimport insthub\n") and "def other():" in path.read_text("utf-8-sig")


def test_names_that_several_folders_share_are_told_apart_by_path(tmp_path):
    folder = tmp_path / "experiment"
    (folder / "recipes").mkdir(parents=True)
    (folder / "insthub-extension.toml").write_text('[extension]\nid = "experiment"\n'
                                                   'recipes = ["recipes", "recipes/lab.py"]\n')
    theirs = folder / "recipes" / "lab.py"
    theirs.write_text("def setup():\n    return 'theirs'\n")
    ext = extensions.load(folder / "insthub-extension.toml")
    assert ext.recipes == (theirs,)  # (named twice: read once)
    mine = write("lab.py", "def setup():\n    return 'mine'\n")
    found, _ = recipes.available([ext])
    assert recipes.find("lab.setup", [ext]).file == mine  # yours first
    assert recipes.runs_as(found[0], found) == "setup" and recipes.runs_as(found[1], found) == f"{theirs}:setup"
    assert recipes.find(f"{theirs}:setup", [ext], any_file=False).origin == "experiment"
    other = tmp_path / "other.py"
    other.write_text("def setup():\n    pass\n")
    with pytest.raises(RecipeError, match="is not a file of recipes insthub lists"):
        recipes.find(f"{other}:setup", [ext], any_file=False)
    assert recipes.find(f"{other}:setup", [ext]).origin == ""  # (insthub run may)
    broken = extensions.load(folder / "insthub-extension.toml")
    broken.error = "it has no Python"
    said = "extension experiment cannot be used (it has no Python): its recipes are not listed"
    assert recipes.available([broken])[1] == [said]


def test_recipes_that_another_user_may_change_are_not_run():
    path = write("lab.py", "def setup():\n    pass\n")
    write("other.py", "def other():\n    pass\n")
    path.chmod(0o666)
    found, problems = recipes.available()
    assert [r.name for r in found] == ["other"]
    said = f"{path}: insthub runs none of its recipes, since every user may change {path} (chmod o-w {path})"
    assert problems == [said]
    path.chmod(0o644)
    recipes.folder().chmod(0o777)
    found, problems = recipes.available()
    assert found == [] and problems[0].startswith(f"{recipes.folder()}: insthub runs none of your recipes, since every")
    with pytest.raises(RecipeError, match="no recipe 'setup' .1 cannot be listed"):
        recipes.find("setup")


def test_run_here_keeps_the_folder_for_imports_made_when_it_runs(server):
    write("helper2.py", "VALUE = 'helper'\n")
    write("lazy.py", "def lazy():\n    import helper2\n    return helper2.VALUE\n")
    assert recipes.run("lazy") == "helper"


def test_the_working_folder_does_not_reach_a_recipe_s_process(tmp_path, monkeypatch):
    (tmp_path / "json.py").write_text("raise SystemExit('the json.py of the working folder ran')\n")
    monkeypatch.chdir(tmp_path)
    write("plain.py", "import json\n\ndef hello():\n    return json.dumps({'a': 1})\n")
    assert recipes.start(recipes.find("hello")).wait(60) == '{"a": 1}'


def test_a_second_stop_leaves_the_cleanup_alone_and_a_result_given_while_stopping_is_kept(tmp_path):
    marker = tmp_path / "cleaned"
    write("slow.py", '''\
        import time
        from pathlib import Path

        def ramp(marker):
            try:
                print("running", flush=True)
                time.sleep(60)
            finally:
                print("ramping down", flush=True)
                time.sleep(0.5)
                Path(marker).write_text("done")

        def stubborn():
            try:
                print("running", flush=True)
                time.sleep(60)
            except KeyboardInterrupt:
                return "kept"
        ''')
    for name, values in (("ramp", {"marker": str(marker)}), ("stubborn", {})):
        run = recipes.start(recipes.find(name), values)
        deadline = time.monotonic() + 30
        while "running" not in run.output() and time.monotonic() < deadline:
            time.sleep(0.02)
        run.stop()
        assert run.stopping
        while "ramping down" not in run.output() and name == "ramp" and time.monotonic() < deadline:
            time.sleep(0.02)
        run.stop()  # (again: does nothing)
        if name == "ramp":
            with pytest.raises(RecipeError, match="was stopped"):
                run.wait(30)
            assert marker.read_text() == "done"
        else:
            assert run.wait(30) == "kept" and not run.stopping


def test_save_keeps_a_linked_file_and_writes_only_python(tmp_path):
    kept = tmp_path / "repo" / "synth.py"
    kept.parent.mkdir()
    kept.write_text("import insthub\n\n\ndef a():\n    return 1\n\n\ndef a():\n    return 2\n")
    recipes.folder().mkdir(parents=True, exist_ok=True)
    (recipes.folder() / "synth.py").symlink_to(kept)
    recipes.save("a", "synth", "rf", (True,))
    assert (recipes.folder() / "synth.py").is_symlink()
    assert kept.read_text().count("def a") == 2 and "return 1" in kept.read_text()  # the last one was replaced
    recipes.save("odd", "synth", 'x"""\n    open("hacked", "w")\n    """')
    problems, notes = recipes.check_file(kept)
    assert problems == [] and [n.split(": ", 1)[1] for n in notes] == ["a is defined again at line 8, which replaces it"]
    assert not (tmp_path / "hacked").exists()
    assert recipes.find("odd").uses == (recipes.Use("synth", 'x"""\n    open("hacked", "w")\n    """', "call", 15),)
    with pytest.raises(ValueError, match="cannot be the name of a parameter"):
        recipes.save("b", "synth", "rf", (), {"class": 1})
    with pytest.raises(ValueError, match="bad device name"):
        recipes.save("b", "../escape", "rf")


# ---------------------------------------------------------------- insthub run, insthub recipes, insthub check
MORE = '''

def typo():
    insthub.device("synth").frequncy = 1
    insthub.device("nosuch").anything()


def array(n=20):
    """An array, and what it printed."""
    print("making", n)
    return np.arange(n) * 1.5


import numpy as np
'''

SLOW = '''\
    import time
    from pathlib import Path

    def wait(marker=""):
        try:
            print("waiting", flush=True)
            time.sleep(60)
        finally:
            if marker:
                Path(marker).write_text("cleaned up")

    def fail(message="boom"):
        print("about to fail")
        raise ValueError(message)
    '''


def main(capture, *argv):
    """insthub ARGV: (exit code, stdout, stderr), with what a recipe's own process printed (capfd)."""
    code = cli.main(list(argv))
    out = capture.readouterr()
    return code, out.out, out.err


def test_insthub_run_checks_a_recipe_then_runs_it_in_a_process_of_its_own(server, capfd, tmp_path):
    write("lab.py", LAB + MORE)
    code, out, err = main(capfd, "run", "setup", "label=cli", "start=5000")
    assert code == 0, err
    assert out.splitlines() == ["lab.setup: Comb measurement. (your recipes) (simulated: synth, sa, loopback)",
                                "cli at 5000 MHz"]
    assert err.endswith(" s)\n") and insthub.device("synth").frequency == 5000  # set from the recipe's process
    code, out, _ = main(capfd, "run", "array")
    assert code == 0 and out.splitlines()[1:] == ["making 20", "<array float64[20] min=0 max=28.5>"]
    target = tmp_path / "a.npy"
    code, out, _ = main(capfd, "run", "lab.array", "n=5", "--save", str(target))
    assert code == 0 and out.endswith(f"saved to {target}\n") and np.array_equal(np.load(target), np.arange(5) * 1.5)

    code, out, _ = main(capfd, "run", "setup", "label=dry", "--dry-run")
    assert code == 0 and out.splitlines()[1:4] == [f"  file      {recipes.folder() / 'lab.py'}:11",
                                                   '  values    label="dry", start=4e3, dbm=0, mode="cw"',
                                                   "  devices   synth, sa, loopback"]
    assert "  uses      synth.frequency = ...  (line 16)\n            synth.power = ...  (line 17)\n" in out
    assert out.endswith("(checked; nothing ran)\n") and insthub.device("synth").frequency == 5000

    code, out, err = main(capfd, "run", "typo")
    assert code == 1 and out == "lab.typo (your recipes) (simulated: synth)\n"
    assert err.splitlines() == ["error: nosuch is not on the bus: start its server",
                                "error: line 38: synth has no property 'frequncy'; did you mean 'frequency'?",
                                "nothing ran"]
    code, _, err = main(capfd, "run", "setup", "dry")
    assert code == 2 and "as NAME=VALUE (recipe lab.setup takes label, start, dbm, mode)" in err
    code, _, err = main(capfd, "run", "setup")
    assert code == 1 and err == "error: recipe setup needs label=...\n"
    assert main(capfd, "run", "stup")[2].startswith("error: no recipe 'stup'; did you mean 'setup'?")

    other = tmp_path / "elsewhere.py"  # any file, by its path
    other.write_text("def hello(who='you'):\n    return f'hello {who}'\n")
    assert main(capfd, "run", f"{other}:hello", "who=lab")[:2] == (0, f"elsewhere.hello ({other})\nhello lab\n")


def test_insthub_run_stops_a_recipe_past_its_timeout_and_says_why_one_failed(capfd, tmp_path):
    marker = tmp_path / "cleaned"
    path = write("slow.py", SLOW)
    start = time.monotonic()
    code, out, err = main(capfd, "run", "wait", f"marker={marker}", "--timeout", "1.5")
    assert code == 1 and out == "slow.wait (your recipes)\nwaiting\n"
    assert err.splitlines() == ["stopping slow.wait: it is still running after 1.5 s (--timeout)",
                                "error: slow.wait was stopped before it finished"]
    assert marker.read_text() == "cleaned up" and time.monotonic() - start < 30  # (its finally block ran)
    code, out, err = main(capfd, "run", "fail", "message=no trigger")
    assert code == 1 and out == "slow.fail (your recipes)\nabout to fail\n"
    assert f'File "{path}", line 14, in fail' in err  # its traceback, from its own process
    assert err.endswith("ValueError: no trigger\nerror: recipe fail failed: ValueError: no trigger\n")


def test_ctrl_c_stops_what_insthub_run_runs(capfd, tmp_path, monkeypatch):
    marker = tmp_path / "cleaned"
    write("slow.py", SLOW)
    monkeypatch.setattr(cli, "_STOP_GRACE", 0.5)  # (Ctrl-C in a terminal reaches the recipe too; this one does not)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)  # (qt.application() of a GUI test changes it)
    try:
        threading.Timer(1.5, os.kill, (os.getpid(), signal.SIGINT)).start()
        code, out, err = main(capfd, "run", "wait", f"marker={marker}")
    finally:
        signal.signal(signal.SIGINT, previous)
    assert code == 130 and out == "slow.wait (your recipes)\nwaiting\n"
    assert err.splitlines() == ["", "stopping slow.wait (Ctrl-C)", "slow.wait was stopped before it finished"]
    assert marker.read_text() == "cleaned up"


class Gun(insthub.Device):
    def open(self):
        self.fired = 0

    @insthub.command(dangerous=True)
    def fire(self):
        self.fired += 1
        return self.fired

    @insthub.command
    def count(self):
        return self.fired


GUN = '''\
    import insthub
    from insthub.recipes import recipe

    def shoot():
        return insthub.device("gun").fire()

    @recipe(dangerous=True)
    def careful():
        return insthub.device("gun").count()
    '''


def test_what_is_dangerous_is_asked_for_and_refused_to_agents(capfd, tmp_path):
    write("gun.py", GUN)
    with insthub.Server("g", [Gun("gun")]) as server:
        gun = server.devices["gun"]
        code, out, err = main(capfd, "run", "shoot")  # (no terminal to ask in)
        assert code == 1 and out == "gun.shoot (your recipes)\n" and gun.fired == 0
        assert err == "error: it uses what is marked dangerous (gun: its command 'fire'): add --yes to run it\n"
        assert "  dangerous gun: its command 'fire'\n" in main(capfd, "run", "shoot", "-n")[1]
        assert main(capfd, "run", "shoot", "--yes")[1].splitlines() == ["gun.shoot (your recipes)", "1"]
        assert gun.fired == 1
        assert "(the recipe says so (@recipe(dangerous=True)))" in main(capfd, "run", "careful")[2]

        tools = mcp.Tools(render.Renderer(tmp_path / "data"), timeout=30)
        listed = json.loads(tools.run("describe", {"device": "gun"}).text)["recipes"]
        assert listed["gun.careful"]["dangerous"] is True and "dangerous" not in listed["gun.shoot"]
        for name, what in (("shoot", "gun: its command 'fire'"), ("careful", "the recipe says so")):
            reply = tools.run("run_recipe", {"name": name})
            assert reply.error and reply.text.startswith(f"PermissionError: recipe gun.{name} uses what is marked "
                                                         f"dangerous ({what}")
            assert f"nothing ran: only the user runs such a recipe (insthub run gun.{name}, or" in reply.text
        assert gun.fired == 1


def test_insthub_recipes_lists_them_by_file(capsys):
    code, out, _ = main(capsys, "recipes")
    assert code == 0 and out.startswith(f"no recipes: write one in {recipes.folder()} (a function in a .py file")
    lab = write("lab.py", LAB)
    synth = write("synth.py", "def off():\n    pass\n\ndef center():\n    pass\n")
    write("broken.py", "def a(:\n")
    write("odd.py", "import math\n\ndef tau(x=2 * math.pi, *, y: int = 1):\n    pass\n")
    code, out, _ = main(capsys, "recipes")
    lines = out.splitlines()
    assert code == 0 and lines[:6] == [f"your recipes: {lab}",
                                       "    setup            Comb measurement.",
                                       '        params   label (required), start=4e3 MHz, dbm=0, mode="cw"',
                                       "        devices  synth, sa, loopback",
                                       "    both             Setup, then where the analyzer is.",
                                       "        params   sa=None"]
    assert "    lab.center\n        devices  sa\n" in out  # center alone names two recipes
    assert f"your recipes: {synth}\n    off\n    synth.center\n" in out
    assert lines[-2] == "not listed:" and lines[-1].startswith(f"    {recipes.folder() / 'broken.py'}:1: ")
    code, out, _ = main(capsys, "recipes", "synth")
    assert [line.split()[0] for line in out.splitlines() if line.startswith("    ") and line[4] != " "][:2] == \
        ["setup", "both"] and "center" not in out
    assert main(capsys, "recipes", "nosuch")[1].startswith("no recipe uses nosuch: write one in")

    code, out, err = main(capsys, "recipes", "--json")
    listed = {r["full_name"]: r for r in json.loads(out)}
    assert code == 0 and "broken.py:1:" in err
    assert set(listed) == {"lab.setup", "lab.both", "lab.center", "odd.tau", "synth.off", "synth.center"}
    setup = listed["lab.setup"]
    assert (setup["name"], setup["title"], setup["file"], setup["line"], setup["origin"], setup["dangerous"],
            setup["error"]) == ("setup", "Comb measurement.", str(lab), 11, "yours", False, None)
    assert setup["devices"] == ["synth", "sa", "loopback"] and setup["doc"].startswith("The synth waits")
    assert setup["params"]["label"] == {"required": True, "annotation": "str", "doc": "what to call it", "meta": {}}
    assert setup["params"]["start"] == {"default": 4000.0, "default_text": "4e3", "required": False,
                                        "annotation": "float", "doc": "", "meta": {"unit": "MHz", "min": 10}}
    assert listed["lab.both"]["params"]["sa"]["default"] is None  # (a default of None is one)
    assert listed["odd.tau"]["params"] == {
        "x": {"default_text": "2 * math.pi", "required": False, "annotation": "", "doc": "", "meta": {}},
        "y": {"default": 1, "default_text": "1", "required": False, "annotation": "int", "doc": "", "meta": {}}}


def test_insthub_check_reads_a_file_of_recipes_without_running_it(capsys):
    lab = write("lab.py", LAB)
    assert main(capsys, "check", str(lab)) == (0, f"{lab}: no problems found; its recipes: setup, both, center\n", "")
    bad = write("bad.py", '''\
        from insthub.recipes import recipe

        @recipe(params={"volt": {"units": "V"}})
        def bad(volt=1, *rest):
            pass

        def good():
            pass
        ''')
    code, out, err = main(capsys, "check", str(bad), "--import")
    assert code == 1 and out.startswith(f"{bad}:4: recipe bad: @recipe params volt") and "did you mean 'unit'" in out
    assert err == f"warning: {bad}:4: recipe bad: *rest stays empty: insthub gives every value by name\n"
    assert main(capsys, "check", str(write("broken.py", "def a(:\n")))[0] == 1


def test_an_extension_s_recipe_files_are_listed_and_checked(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "experiment"
    (folder / "recipes").mkdir(parents=True)
    (folder / "insthub-extension.toml").write_text('[extension]\nid = "experiment"\nrecipes = ["recipes", "broken.py"]\n')
    (folder / "recipes" / "lab.py").write_text("def setup():\n    pass\n\ndef theirs():\n    pass\n")
    (folder / "broken.py").write_text("def a(:\n")
    monkeypatch.setenv("INSTHUB_EXTENSIONS", str(folder))
    code, out, _ = main(capsys, "ext", "list")
    assert code == 0 and f"    recipes  recipes/lab.py (setup, theirs), broken.py (cannot be read: insthub check " \
                         f"{folder / 'broken.py'} says why)\n" in out
    code, out, _ = main(capsys, "check", str(folder))
    assert code == 1 and out.startswith(f"{folder / 'broken.py'}:1: ")
    (folder / "broken.py").unlink()  # (then the manifest names a file that is not there: a warning)
    code, out, err = main(capsys, "check", str(folder))
    assert code == 0 and out == f"{folder}: no problems found\n" and "broken.py is neither" in err  # no drivers


def test_agents_see_the_recipes_of_a_device_and_run_them(server, tmp_path):
    path = write("lab.py", LAB + MORE)
    write("slow.py", SLOW)
    tools = mcp.Tools(render.Renderer(tmp_path / "data"), timeout=30)

    def ok(tool, /, **arguments):
        reply = tools.run(tool, arguments)
        assert not reply.error, reply.text
        return json.loads(reply.text)

    def failed(tool, /, **arguments):
        reply = tools.run(tool, arguments)
        assert reply.error, reply.text
        return reply.text

    listed = ok("describe", device="synth")["recipes"]
    assert set(listed) == {"lab.setup", "lab.both", "lab.typo"} and listed["lab.both"]["params"] == {
        "sa": {"default": None}}
    assert listed["lab.setup"] == {
        "title": "Comb measurement.", "doc": "The synth waits at start; the analyzer looks at it.",
        "params": {"label": {"required": True, "annotation": "str", "doc": "what to call it"},
                   "start": {"default": 4000.0, "annotation": "float", "meta": {"unit": "MHz", "min": 10}},
                   "dbm": {"default": 0, "annotation": "float"},
                   "mode": {"default": "cw", "annotation": 'Literal["cw", "sweep"]',
                            "meta": {"choices": ["cw", "sweep"]}}},
        "file": f"{path}:11", "devices": ["synth", "sa", "loopback"]}
    assert "recipes" not in ok("describe", device="camera")

    done = ok("run_recipe", name="setup", params={"label": "agent", "start": 3000})
    assert (done["recipe"], done["result"], done["simulated"]) == ("lab.setup", "agent at 3000 MHz",
                                                                   ["synth", "sa", "loopback"])
    assert "output" not in done and 0 < done["seconds"] < 30 and insthub.device("synth").frequency == 3000
    done = ok("run_recipe", name="lab.array", params={"n": 100})
    assert done["output"] == "making 100\n" and done["result"]["array"] == "float64[100]" and "simulated" not in done
    assert np.array_equal(np.load(done["result"]["file"]), np.arange(100) * 1.5)

    text = failed("run_recipe", name="typo")
    assert text.startswith("RecipeError: recipe lab.typo cannot run, so nothing ran: nosuch is not on the bus")
    assert "has no property 'frequncy'" in text
    assert failed("run_recipe", name="setup") == "RecipeError: recipe setup needs label=..."
    assert "unexpected keyword argument 'device'" in failed("run_recipe", name="setup", device="synth")
    text = failed("run_recipe", name="fail")
    assert text.startswith("RecipeError: recipe fail failed: ValueError: boom\n--- what it printed (the end) ---\n"
                           "about to fail\nTraceback") and text.endswith("ValueError: boom")
    start = time.monotonic()
    text = failed("run_recipe", name="wait", timeout=1.5)
    assert text.startswith("TimeoutError: recipe slow.wait was still running after 1.5 s, so it was stopped")
    assert text.endswith("---\nwaiting") and time.monotonic() - start < 20


HOLD = '''\
    import time
    from pathlib import Path

    def hold(folder):
        Path(folder, "started").write_text("")
        try:
            time.sleep(60)
        finally:
            Path(folder, "stopped").write_text("")

    def until_stopped():
        print("measuring", flush=True)
        try:
            while True:
                time.sleep(0.05)
        except KeyboardInterrupt:
            print("interrupted: returning what it has")
            return "what it had"
    '''


def wait_for(path, timeout=30.0):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} did not appear"
        time.sleep(0.02)


def test_agents_run_only_the_recipes_describe_lists(tmp_path):
    marker = tmp_path / "ran"
    mine = write("plain.py", "def hello(who='you'):\n    return f'hello {who}'\n")
    write("_helpers.py", f"from pathlib import Path\n\ndef secret():\n    Path({str(marker)!r}).write_text('ran')\n")
    outside = tmp_path / "outside.py"
    outside.write_text(f"from pathlib import Path\n\ndef go():\n    Path({str(marker)!r}).write_text('ran')\n")
    tools = mcp.Tools(render.Renderer(tmp_path / "data"), timeout=30)
    for name in (f"{outside}:go", f"{recipes.folder() / '_helpers.py'}:secret", f"{os.__file__}:getcwd"):
        reply = tools.run("run_recipe", {"name": name})
        assert reply.error and reply.text.startswith(f"PermissionError: {name.rpartition(':')[0]} is not a file of the "
                                                     "recipes describe lists"), reply.text
        assert f"nothing ran: only the user runs a function of another file (insthub run {name})" in reply.text
    assert not marker.exists()
    reply = tools.run("run_recipe", {"name": f"{mine}:hello", "params": {"who": "lab"}})  # a listed file: its path
    assert not reply.error and json.loads(reply.text)["result"] == "hello lab"
    assert "has no recipe 'nope'" in tools.run("run_recipe", {"name": f"{mine}:nope"}).text


def test_a_recipe_with_the_full_name_of_another_is_named_by_its_path(server, tmp_path, monkeypatch, capfd):
    folder = tmp_path / "experiment"
    (folder / "recipes").mkdir(parents=True)
    (folder / "insthub-extension.toml").write_text('[extension]\nid = "experiment"\nrecipes = ["recipes"]\n')
    theirs = folder / "recipes" / "lab.py"
    theirs.write_text('import insthub\n\ndef setup(theirs=1):\n    """Theirs."""\n    insthub.device("synth").power\n'
                      '    return ["theirs", theirs]\n')
    write("lab.py", 'import insthub\n\ndef setup(mine=2):\n    """Mine."""\n    insthub.device("synth").power\n'
                    '    return ["mine", mine]\n')
    monkeypatch.setenv("INSTHUB_EXTENSIONS", str(folder))
    tools = mcp.Tools(render.Renderer(tmp_path / "data"), timeout=30)
    listed = json.loads(tools.run("describe", {"device": "synth"}).text)["recipes"]
    assert list(listed) == ["lab.setup", f"{theirs}:setup"]  # yours first, by the name that runs it
    assert listed["lab.setup"]["title"] == "Mine." and listed[f"{theirs}:setup"]["title"] == "Theirs."
    reply = tools.run("run_recipe", {"name": "lab.setup", "params": {"mine": 3}})
    assert json.loads(reply.text)["result"] == ["mine", 3]
    reply = tools.run("run_recipe", {"name": f"{theirs}:setup", "params": {"theirs": 5}})
    assert json.loads(reply.text)["result"] == ["theirs", 5]

    code, out, _ = main(capfd, "recipes")
    assert code == 0 and "    setup            Mine.\n" in out and f"    {theirs}:setup Theirs.\n" in out
    code, out, _ = main(capfd, "run", f"{theirs}:setup", "theirs=4")
    assert code == 0 and out.startswith("lab.setup: Theirs. (extension experiment)") and '"theirs",\n  4' in out


def test_agents_are_told_who_paused_a_device_a_recipe_needs(server, tmp_path):
    import re

    write("lab.py", LAB)
    tools = mcp.Tools(render.Renderer(tmp_path / "data"), timeout=30)
    server.device("synth").bus.pause(by="Alice, aligning by hand")
    try:
        reply = tools.run("run_recipe", {"name": "setup", "params": {"label": "x"}})
        assert reply.error and re.match(r"RecipeError: recipe lab.setup cannot run, so nothing ran: synth is paused "
                                        r"\(since \d\d:\d\d:\d\d by Alice, aligning by hand\)\. A person paused it",
                                        reply.text), reply.text
        assert reply.text.endswith("do not resume it unless they ask you to.") and "resume it first" not in reply.text
    finally:
        server.device("synth").bus.resume()
    assert not tools.run("call", {"device": "synth", "command": "$pause"}).error
    try:
        text = tools.run("run_recipe", {"name": "setup", "params": {"label": "x"}}).text
        assert "synth is paused (since " in text and "by an AI agent (insthub mcp)). An AI agent paused it" in text
    finally:
        server.device("synth").bus.resume()


SAVED = '''\
    import numpy as np

    def trace():
        t = np.linspace(0, 1e-3, 1000)
        return t, np.sin(t)

    def keyed():
        return {1: np.arange(3), "file": np.ones(2), "allow_pickle": "x", "note": None}

    def clash():
        return {1: np.arange(2), "1": [5, 6]}
    '''


def test_insthub_run_saves_the_data_of_a_tuple_or_a_dict(capfd, tmp_path):
    write("saved.py", SAVED)
    code, out, _ = main(capfd, "run", "trace", "--save", str(tmp_path / "trace"))
    assert code == 0 and out.endswith(f"saved to {tmp_path / 'trace.npz'} (arr_0, arr_1)\n")
    with np.load(tmp_path / "trace.npz") as saved:
        assert np.array_equal(saved["arr_1"], np.sin(np.linspace(0, 1e-3, 1000))) and saved["arr_0"][-1] == 1e-3
    code, out, _ = main(capfd, "run", "keyed", "--save", str(tmp_path / "keyed.npz"))
    assert code == 0 and out.endswith("(1, file, allow_pickle, note)\n")  # (keys np.savez(**it) cannot take)
    with np.load(tmp_path / "keyed.npz", allow_pickle=True) as saved:
        assert np.array_equal(saved["1"], [0, 1, 2]) and saved["allow_pickle"] == "x" and saved["note"].item() is None
    code, out, err = main(capfd, "run", "clash", "--save", str(tmp_path / "clash.npz"))
    assert code == 1 and json.loads(out.split("\n", 1)[1]) == {"1": [5, 6]}  # (printed, not lost)
    assert err.splitlines()[-2] == (f"error: cannot save it to {tmp_path / 'clash.npz'}: ValueError: two of its "
                                    "keys are both '1' as text (it is printed above)")
    assert not (tmp_path / "clash.npz").exists()
    code, out, err = main(capfd, "run", "trace", "--save", str(tmp_path / "no" / "such" / "folder.npz"))
    assert code == 1 and "<array float64[1000]" in out and "error: cannot save it to " in err


def test_what_a_stopped_recipe_returns_is_kept(capfd, tmp_path):
    write("hold.py", HOLD)
    code, out, err = main(capfd, "run", "until_stopped", "--timeout", "2")
    assert code == 0 and out == ("hold.until_stopped (your recipes)\nmeasuring\ninterrupted: returning what it has\n"
                                 "what it had\n")
    assert err.startswith("stopping hold.until_stopped: it is still running after 2 s (--timeout)\n(")
    assert err.endswith(" s; it returned this once --timeout stopped it)\n")
    tools = mcp.Tools(render.Renderer(tmp_path / "data"), timeout=30)
    reply = tools.run("run_recipe", {"name": "until_stopped", "timeout": 2})
    assert not reply.error, reply.text
    done = json.loads(reply.text)
    assert done["result"] == "what it had" and done["output"].endswith("interrupted: returning what it has\n")
    assert done["stopped"] == "it was still running after 2 s, so it was stopped; this is what it returned then"


def test_insthub_run_asks_on_stderr(capfd, monkeypatch):
    import io
    import sys

    write("gun.py", GUN)
    monkeypatch.setattr(cli, "_can_ask", lambda: True)
    with insthub.Server("g", [Gun("gun")]) as server:
        monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
        code, out, err = main(capfd, "run", "shoot")
        assert code == 1 and out == "gun.shoot (your recipes)\n" and server.devices["gun"].fired == 0
        assert err == "It uses what is marked dangerous (gun: its command 'fire'). Run it? [y/N] nothing ran\n"
        monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
        code, out, err = main(capfd, "run", "shoot")  # stdout: only the recipe's (as with --yes)
        assert code == 0 and out == "gun.shoot (your recipes)\n1\n" and err.startswith("It uses what is marked")
        assert server.devices["gun"].fired == 1


def test_a_dry_run_says_in_which_file_a_use_is(server, capfd):
    write("analyzer.py", '''\
        import insthub

        sa = insthub.device("sa")


        def centre(hz):
            sa.center = hz
        ''')
    write("comb.py", "from analyzer import centre\n\n\ndef scan():\n    centre(4e9)\n")
    code, out, _ = main(capfd, "run", "scan", "--dry-run")
    assert code == 0 and "  uses      sa.center = ...  (analyzer.py line 7)\n" in out


def test_insthub_recipes_json_is_valid_json_whatever_the_defaults(capsys):
    write("big.py", "from typing import Literal\n\n"
                    "def big(limit=1e999, table={(1, 2): 3}, mode: Literal[1e999, 2] = 2, ok=1.5):\n    pass\n")

    def no_constant(name):
        raise AssertionError(f"{name} is not JSON")

    code, out, _ = main(capsys, "recipes", "--json")
    params = json.loads(out, parse_constant=no_constant)[0]["params"]
    assert code == 0 and "default" not in params["limit"] and params["limit"]["default_text"] == "1e999"
    assert "default" not in params["table"] and params["table"]["default_text"] == "{(1, 2): 3}"
    assert params["mode"]["meta"] == {"choices": ["inf", 2]} and params["ok"]["default"] == 1.5
    code, out, _ = main(capsys, "recipes")
    assert code == 0 and "limit=1e999, table={(1, 2): 3}, mode=2, ok=1.5" in out


def test_one_recipe_insthub_cannot_read_leaves_the_commands_working(capsys):
    typo = write("typo.py", "from insthub.recipes import recipe\n\n@recipe(params={{'x': 1}})\ndef bad(x=1):\n"
                            "    pass\n")
    write("plain.py", "def hello(who='you'):\n    return f'hello {who}'\n")
    code, out, _ = main(capsys, "recipes")
    assert code == 0 and "    bad\n" in out and "        error    " in out and "    hello\n" in out
    assert main(capsys, "run", "hello", "who=lab")[:2] == (0, "plain.hello (your recipes)\nhello lab\n")
    code, out, err = main(capsys, "check", str(typo))
    assert code == 1 and out.startswith(f"{typo}:4: recipe bad: ") and "Traceback" not in out + err


def test_insthub_check_of_a_relative_path_does_not_say_it_hides_itself(capsys, monkeypatch):
    path = write("relpath_lab.py", "def hello():\n    pass\n")
    monkeypatch.chdir(path.parent)
    monkeypatch.syspath_prepend(str(path.parent))  # (as `python -m insthub` has the working folder)
    assert main(capsys, "check", "relpath_lab.py") == (0, "relpath_lab.py: no problems found; its recipes: hello\n",
                                                       "")


def test_a_recipe_an_agent_runs_is_stopped_when_its_request_is_cancelled_or_the_server_exits(tmp_path):
    write("hold.py", HOLD)
    tools = mcp.Tools(render.Renderer(tmp_path / "data"), timeout=600)

    def hold(folder, cancelled, replies):
        replies.append(tools.run("run_recipe", {"name": "hold", "params": {"folder": str(folder)}}, cancelled))

    for how in ("cancel", "close", "after close"):
        folder = tmp_path / how.replace(" ", "_")
        folder.mkdir()
        cancelled, replies = threading.Event(), []
        thread = threading.Thread(target=hold, args=(folder, cancelled, replies))
        thread.start()
        if how != "after close":
            wait_for(folder / "started")
        if how == "cancel":
            cancelled.set()
            tools.cancel(cancelled)
        elif how == "close":
            tools.close()
        thread.join(30)
        assert replies and replies[0].error and "recipe hold was stopped" in replies[0].text, how
        if (folder / "started").exists():
            assert (folder / "stopped").exists(), how  # (its finally block ran)
    assert tools._runs == {}
