"""`import insthub` stays light, because every lab program runs it: see
docs/design.md#keeping-the-core-light.  A fresh Python imports it with every
folder insthub may use pointed at an empty one, and what it loaded is checked."""
import json
import os
import subprocess
import sys

#: What `import insthub` must not load (a module, or any module of that package).
FORBIDDEN = ("PyQt5", "PyQt6", "PySide2", "PySide6", "pyqtgraph", "matplotlib", "importlib.metadata", "asyncio")

#: The only insthub modules it may load: the core.  `_version` holds one line, insthub's version, in a
#: module of its own so that pyproject.toml and the server's hello read it from one place.
CORE = {"insthub", "insthub.device", "insthub.server", "insthub.client", "insthub.codec", "insthub.wire",
        "insthub.registry", "insthub.errors", "insthub._version"}

#: The only packages besides the standard library.  `cython_runtime` is not one: Cython-compiled
#: extensions (msgpack's) register a helper module of that name.
THIRD_PARTY = {"numpy", "msgpack", "cython_runtime"}

SNIPPET = """
import json, sys, time
sys.path.insert(0, sys.argv[1])  # this checkout's insthub, installed or not
before = set(sys.modules)
start = time.perf_counter()
import insthub
seconds = time.perf_counter() - start
print(json.dumps({"added": sorted(set(sys.modules) - before), "seconds": seconds}))
"""


def _import_insthub(tmp_path) -> dict:
    empty = tmp_path / "empty"
    empty.mkdir()
    env = {**os.environ, "HOME": str(empty), "INSTHUB_DIR": str(empty / "run"),
           "XDG_RUNTIME_DIR": str(empty / "xdg"), "XDG_CONFIG_HOME": str(empty / "config"),
           "INSTHUB_DEVICES": str(empty / "devices.toml")}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-I", "-c", SNIPPET, root], env=env, capture_output=True, text=True,
                         timeout=60, check=True).stdout
    return {**json.loads(out.splitlines()[-1]), "folder": sorted(p.name for p in empty.iterdir())}


def test_import_insthub_loads_only_the_core(tmp_path):
    got = _import_insthub(tmp_path)
    added = got["added"]
    print(f"\nimport insthub: {got['seconds'] * 1000:.0f} ms, {len(added)} modules")
    assert not [m for m in added if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)]
    assert {m for m in added if m.split(".")[0] == "insthub"} <= CORE
    third_party = {m.split(".")[0] for m in added} - set(sys.stdlib_module_names) - {"insthub"}
    assert {name for name in third_party if not name.startswith("_")} <= THIRD_PARTY
    assert got["folder"] == []  # it wrote nothing: no runtime folder, no config; it connected nowhere
