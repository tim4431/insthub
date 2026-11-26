"""Recipes: snippets you keep as Python functions, which insthub lists and runs by name (see
docs/design.md#recipes).

A recipe is a public function of a recipes file:

    # ~/.config/insthub/recipes/redpitaya.py
    import insthub

    def square(freq: float = 1e3, volt: float = 0.5, chan: int = 1):
        \"\"\"Square wave out of OUT{chan}.\"\"\"
        rp = insthub.device("redpitaya")
        rp.sour_set(chan, func="SQUARE", volt=volt, freq=freq)
        rp.tx_txt(f"OUTPUT{chan}:STATE ON")

insthub reads a file with `ast` and never runs it to list or check its recipes.  It reads each
function's name, docstring and parameters (defaults, annotations, `Literal[...]` choices, and
what an optional `@recipe(...)` adds), the devices it uses (`insthub.device("name")` with the
name written out), and what it uses of them (`rp.sour_set(...)`, `rp.power = 3`,
`rp.call("name")`).  It follows what runs when the recipe runs: the file's top level, the
functions of the file it calls, and those of recipes files beside it that it imports.  So the
window can list recipes without importing anything, and check() can ask the devices before a
recipe runs: whether they are there, whether they have what it uses, and what of it is marked
dangerous.  What it cannot read (a name known only when the code runs) it says so.

Running a recipe is calling its function: in this process (load(), run()), or in a process of
its own (start()), which `insthub run`, the window and the agents' server use, so that a recipe
that hangs or crashes never takes them along, and Stop ends it.

    folder()                       your recipes: ~/.config/insthub/recipes/*.py ($INSTHUB_RECIPES)
    read_file(path)                the recipes of one file, read without running it
    available()                    every recipe this computer knows of: yours, then extensions'
    for_device(name)               the recipes that use a device
    find(name)                     one by name: "square", "redpitaya.square", "path/to/file.py:square"
    check(recipe)                  ask its devices: what stops it, what is dangerous, what is simulated
    load(recipe), run(name, **kw)  its function, as the file is now / call it here
    start(recipe, values)          run it in a process of its own: a Run (output, stop(), wait())
    save(name, device, command, args, kwargs)   one call, as a recipe in your folder (save_path())

`import insthub` never imports this module.
"""
from __future__ import annotations

import ast
import dataclasses
import keyword
import os
import sys
import threading
import time
from pathlib import Path
from typing import NamedTuple

from . import registry
from .client import Bus
from .device import _checked_meta
from .errors import BusError, DeviceNotFound, did_you_mean

ENV = "INSTHUB_RECIPES"  #: your recipes folder, instead of ~/.config/insthub/recipes
YOURS = "yours"  #: the origin of a recipe of yours (else: its extension's id; "" for a file named directly)
_NEW_FILE = ('"""My insthub recipes (docs/guide.md#recipes): each public function is one.\n\n'
             '`insthub run NAME` runs one; insthub gui lists them, each also on the page of the devices it uses.\n'
             '"""\nimport insthub\n')
_DECORATOR_KEYS = ("params", "dangerous")
_OUTPUT_KEPT = 200_000  #: characters of a Run's output kept (the last ones)
_LITERAL_ERRORS = (ValueError, TypeError, SyntaxError, MemoryError, RecursionError)  # of ast.literal_eval
_BUS_NAMED = {"call": "call", "future": "call", "get": "get", "set": "set"}  #: dev.bus.<this>("name", ...)
#: The process of start(): without the working folder on sys.path, whose modules would come first.
_CHILD = "import sys; del sys.path[0]; from insthub.recipes import _child; sys.exit(_child(sys.argv[1:]))"


class _Sentinel:
    def __init__(self, text: str):
        self._text = text

    def __repr__(self) -> str:
        return self._text

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


_MISSING = _Sentinel("(required)")  #: the default of a parameter that has none
_UNREAD = _Sentinel("(not a literal)")  #: the default of one whose default is not written as a literal
_LATER = _Sentinel("(named when it runs)")  #: a device whose name is known only when the code runs


class RecipeError(BusError):
    """A recipe that cannot be found, used or run, or that failed; `output`: what it printed."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


@dataclasses.dataclass(frozen=True)
class Param:
    """A parameter of a recipe: its default (if the file writes it as a literal), as the file writes
    it and its annotation, and the metadata a device property has (unit, min, max, step, choices:
    advice for forms, not enforced)."""

    name: str
    default: object = _MISSING  #: _MISSING: it must be given; _UNREAD: it has one, not written as a literal
    default_text: str = ""  #: as the file writes it ("1e3")
    annotation: str = ""  #: as the file writes it ("float", "Literal['A', 'B']")
    doc: str = ""
    meta: dict = dataclasses.field(default_factory=dict)

    @property
    def required(self) -> bool:
        return self.default is _MISSING

    @property
    def literal(self) -> bool:
        """Whether it has a default written as a literal, so that `default` is that value."""
        return self.default is not _MISSING and self.default is not _UNREAD

    def text(self) -> str:
        """As a list shows it: "freq=1e3 Hz", "chan (required)"."""
        if self.required:
            return f"{self.name} (required)"
        unit = self.meta.get("unit")
        return f"{self.name}={self.default_text}" + (f" {unit}" if unit else "")


@dataclasses.dataclass(frozen=True)
class Use:
    """What a recipe uses of a device: `kind` "call" (a command, or a method of the proxy), "get"
    (reading a property) or "set" (writing one), at `line` of its file, or of the recipes file
    beside it called `file`."""

    device: str
    name: str
    kind: str
    line: int
    file: str = ""

    def text(self) -> str:
        return f"{self.device}.{self.name}" + {"call": "()", "set": " = ...", "get": ""}[self.kind]

    def where(self) -> str:
        """"line 12", or "redpitaya.py line 12" for a file beside it."""
        return f"{self.file} line {self.line}" if self.file else f"line {self.line}"


@dataclasses.dataclass(frozen=True)
class Recipe:
    """A recipe as its file says, read without running it.  `group` is its file's name without .py
    (the recipe is also called group.name); `origin` is YOURS, an extension's id, or "" for a file
    named directly; `python`, the Python its own process runs in (None: the one running insthub).
    One that cannot be used says why in `error`: it never makes the other recipes of its file
    unusable."""

    name: str
    group: str = ""
    file: Path | None = None
    line: int = 0  #: of its `def`
    span: tuple = (0, 0)  #: its first line (its decorators') and last line
    title: str = ""
    doc: str = ""
    params: dict = dataclasses.field(default_factory=dict)  #: name -> Param, in the file's order
    takes_more: bool = False  #: it has **kwargs: values it has no parameter for are passed on too
    devices: tuple = ()  #: the devices its functions name, as read from its files
    at_import: tuple = ()  #: the devices the top levels of its files name: it needs them to start at all
    uses: tuple = ()  #: Use: what runs uses of its devices, its own file's first
    named_later: bool = False  #: it also names a device, command or property only when it runs
    dangerous: bool = False  #: @recipe(dangerous=True)
    origin: str = YOURS
    python: str | None = None
    error: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.group}.{self.name}"

    def where(self) -> str:
        """Where it is from, as a message says it: "your recipes", "extension windfreak", or its file."""
        if self.origin == YOURS:
            return "your recipes"
        return f"extension {self.origin}" if self.origin else str(self.file)

    def source(self) -> str:
        """Its code, as its file is now ("" if the file cannot be read)."""
        try:
            lines = _decoded(self.file.read_bytes()).splitlines()
        except (OSError, ValueError, SyntaxError, AttributeError):
            return ""
        return "\n".join(lines[self.span[0] - 1:self.span[1]])


def _decoded(data: bytes) -> str:
    """A file's text, as Python reads its source: a BOM and a coding line are honoured."""
    import importlib.util

    return importlib.util.decode_source(data)


# ---------------------------------------------------------------- reading
_cache: dict[Path, tuple] = {}  # file -> ((mtime, size), _File: what the file alone says)


class _Ref(NamedTuple):
    """A name imported from a recipes file beside it (from devices import laser): a device if that
    file's top level names one so."""

    file: Path
    name: str


@dataclasses.dataclass
class _Facts:
    """What one function, or a file's top level, says alone: the devices it names ((line, name)),
    what it uses of them (Use; the device may still be a _Ref), the functions of its file it calls,
    the functions of files beside it that it calls ((file, name)), the files beside it that it
    imports (their top level runs), and whether it names something only when it runs."""

    devices: list = dataclasses.field(default_factory=list)
    uses: list = dataclasses.field(default_factory=list)
    calls: list = dataclasses.field(default_factory=list)
    beside: list = dataclasses.field(default_factory=list)
    imports: list = dataclasses.field(default_factory=list)
    later: bool = False


@dataclasses.dataclass
class _File:
    """What a recipes file says alone: its recipes (without what they use), what makes the file
    unusable, notes, the facts of each function and of its top level, and the devices its top
    level names (name -> device, or a _Ref)."""

    recipes: list
    problems: list
    notes: list
    functions: dict = dataclasses.field(default_factory=dict)
    top: _Facts = dataclasses.field(default_factory=_Facts)
    globals: dict = dataclasses.field(default_factory=dict)


def read_file(path: str | Path, origin: str = YOURS, python: str | None = None) -> tuple[list[Recipe], list[str]]:
    """The recipes of a .py file, read without running it: one per public top-level function, in the
    file's order; and what makes the whole file unusable (it cannot be read, or is not Python).
    What makes one recipe unusable is in its `error`."""
    recipes, problems, _ = _read(Path(path).expanduser())
    return [dataclasses.replace(r, origin=origin, python=python) for r in recipes], problems


def check_file(path: str | Path) -> tuple[list[str], list[str]]:
    """What `insthub check` says of a recipes file: (what makes it or one of its recipes unusable,
    notes on what is probably a mistake or cannot be checked)."""
    path = Path(path).expanduser()
    recipes, problems, notes = _read(path)
    notes += [f"{path}:{r.line}: recipe {r.name}: it names a device, command or property only when it runs, which "
              "is not checked before it runs" for r in recipes if r.named_later]
    return problems + [f"{path}:{r.line}: recipe {r.name}: {r.error}" for r in recipes if r.error], notes


def _read(path: Path) -> tuple[list[Recipe], list[str], list[str]]:
    """(its recipes, with what they use; what makes the file unusable; notes).  What each file says
    alone is cached while it does not change; what a recipe uses across the files beside it is
    put together each time, so that a change there, or a file that appears, is seen at once."""
    file = _file(path)
    if file is None:
        return [], [f"{path}: no such file"], []
    recipes = []
    for r in file.recipes:
        devices, at_import, uses, later = _used(path, r.name)
        recipes.append(dataclasses.replace(r, devices=tuple(devices), at_import=tuple(at_import), uses=tuple(uses),
                                           named_later=later))
    return recipes, list(file.problems), list(file.notes)


def _stamp(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _file(path: Path) -> _File | None:
    """What a file says alone (None: there is no such file), from the cache while it is unchanged."""
    stamp = _stamp(path)
    if stamp is None:
        return None
    cached = _cache.get(path)
    if cached is None or cached[0] != stamp:
        cached = _cache[path] = stamp, _parse(path)
    return cached[1]


def _used(origin: Path, name: str) -> tuple[list, list, list, bool]:
    """What runs when the recipe `name` of the file `origin` runs: the file's top level (it is
    imported), the function, the functions of its file it calls, those of files beside it that it
    calls, and the top levels of those files (they are imported).  Returns (the devices functions
    name, the devices top levels name, the uses, whether something is named only when it runs)."""
    devices, at_import, uses, later = [], [], [], False
    todo, seen = [(origin, None), (origin, name)], set()
    while todo:
        item = todo.pop(0)
        if item in seen:
            continue
        seen.add(item)
        path, function = item
        file = _file(path)
        facts = None if file is None else file.top if function is None else file.functions.get(function)
        if facts is None:  # (no such file, or a name of it that is no function: a device it names is a _Ref)
            continue
        if function is not None and path != origin:
            todo.append((path, None))  # it was imported, so its top level ran
        for _, device in facts.devices:
            if found := _resolved(device):
                (at_import if function is None else devices).append(found)
        for use in facts.uses:
            if found := _resolved(use.device):
                uses.append(dataclasses.replace(use, device=found, file="" if path == origin else path.name))
        later = later or facts.later
        todo += [(path, c) for c in facts.calls] + facts.beside + [(f, None) for f in facts.imports]
    uses.sort(key=lambda u: (u.file, u.line))
    return list(dict.fromkeys(devices)), list(dict.fromkeys(at_import)), list(dict.fromkeys(uses)), later


def _resolved(device) -> str | None:
    """A device's name: as it is, or what a _Ref names in its file (None if it names no device)."""
    for _ in range(8):  # (a _Ref to a _Ref: a name imported again from one more file)
        if not isinstance(device, _Ref):
            break
        file = _file(device.file)
        device = None if file is None else file.globals.get(device.name)
    return device if isinstance(device, str) else None


def _parse(path: Path) -> _File:
    try:
        source = _decoded(path.read_bytes())
        tree = ast.parse(source, str(path))
    except SyntaxError as e:
        return _File([], [f"{path}:{e.lineno}: {e.msg} (so none of its recipes can be used)"], [])
    except (OSError, ValueError, LookupError, MemoryError, RecursionError) as e:  # (LookupError: a coding unknown)
        return _File([], [f"{path}: cannot be read: {e}"], [])
    try:
        module = _Module(tree, path)
    except Exception as e:  # noqa: BLE001 - code insthub cannot follow: then it cannot check any of its recipes
        return _File([], [f"{path}: insthub cannot read it ({type(e).__name__}: {e})"], [])
    notes = []
    if _hides_module(path):
        notes.append(f"{path}: while a recipe of its folder runs, `import {path.stem}` finds this file instead of the "
                     f"module {path.stem}: rename the file")
    seen = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in seen:
                notes.append(f"{path}:{seen[node.name]}: {node.name} is defined again at line {node.lineno}, "
                             "which replaces it")
            seen[node.name] = node.lineno
    public = [n for n in module.functions.values() if not n.name.startswith("_")]
    if not public:
        notes.append(f"{path}: it has no public function, so no recipe (a name that starts with _ is private)")
    recipes = []
    for node in public:
        try:
            recipe, said = _recipe(node, path, source)
        except Exception as e:  # noqa: BLE001 - what insthub cannot read makes this recipe unusable, not the others
            recipe = Recipe(node.name, path.stem, path, node.lineno, (node.lineno, node.end_lineno), node.name,
                            error=f"insthub cannot read it ({type(e).__name__}: {e})")
            said = []
        recipes.append(recipe)
        notes += [f"{path}:{node.lineno}: recipe {node.name}: {note}" for note in said]
    return _File(recipes, [], notes, module.facts, module.top, module.globals)


def _hides_module(path: Path) -> bool:
    """Whether a recipes file is named as a module that can be imported (time.py, numpy.py), which it
    hides from the recipes of its folder while they run (their folder is first on sys.path)."""
    import importlib.machinery

    if not path.stem.isidentifier():
        return False
    if path.stem in sys.stdlib_module_names:
        return True
    try:  # (in the folders the program has, not those of recipes run here, which hold such files)
        spec = importlib.machinery.PathFinder.find_spec(path.stem, [p for p in sys.path if p not in _added])
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.origin:
        return spec is not None
    try:
        return Path(spec.origin).resolve() != path.resolve()
    except OSError:
        return True


def _recipe(node, path: Path, source: str) -> tuple[Recipe, list[str]]:
    problems, notes = [], []
    first = min([d.lineno for d in node.decorator_list] + [node.lineno])
    doc = ast.get_docstring(node) or ""
    title, _, rest = doc.partition("\n")
    recipe = Recipe(node.name, path.stem, path, node.lineno, (first, node.end_lineno), title.strip() or node.name,
                    rest.strip())
    if isinstance(node, ast.AsyncFunctionDef):
        problems.append("it is an async def: a recipe is a plain function")
    args = node.args
    if args.posonlyargs:
        problems.append("its parameters before / cannot be given by name, and insthub gives every value by name")
    if args.vararg:
        notes.append(f"*{args.vararg.arg} stays empty: insthub gives every value by name")
    decorated, problem = _decorator(node)
    if problem:
        problems.append(problem)
    params = {}
    plain = args.args
    defaults = [None] * (len(plain) - len(args.defaults)) + list(args.defaults)
    for arg, default in [*zip(plain, defaults, strict=True), *zip(args.kwonlyargs, args.kw_defaults, strict=True)]:
        param, problem = _param(arg, default, decorated.get("params", {}).get(arg.arg), source)
        params[arg.arg] = param
        if problem:
            problems.append(problem)
    unknown = [p for p in decorated.get("params", {}) if p not in params]
    if unknown:
        problems.append(f"@recipe params names {unknown[0]!r}, which is not one of its parameters" +
                        did_you_mean(str(unknown[0]), params))
    recipe = dataclasses.replace(recipe, params=params, takes_more=args.kwarg is not None,
                                 dangerous=bool(decorated.get("dangerous", False)))
    if problems:
        recipe = dataclasses.replace(recipe, error="; ".join(problems))
    return recipe, notes


def _decorator(node) -> tuple[dict, str | None]:
    """What @recipe(...) says: {"params": ..., "dangerous": ...}, read as literals; and a problem."""
    for d in node.decorator_list:
        call = d if isinstance(d, ast.Call) else None
        target = call.func if call else d
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if name != "recipe":
            continue
        if call is None:
            return {}, None
        if call.args:
            return {}, "@recipe takes only params={...} and dangerous=True"
        found = {}
        for kw in call.keywords:
            if kw.arg not in _DECORATOR_KEYS:
                return {}, f"@recipe has no {kw.arg}=" + did_you_mean(kw.arg or "", _DECORATOR_KEYS)
            try:
                found[kw.arg] = ast.literal_eval(kw.value)
            except _LITERAL_ERRORS:
                return {}, (f"@recipe({kw.arg}=...) is read without running the file, so write it out as a literal, "
                            'as params={"freq": {"unit": "Hz"}}')
        if not isinstance(found.get("params", {}), dict) or \
                not all(isinstance(v, dict) for v in found.get("params", {}).values()):
            return {}, '@recipe params should be a dict of dicts, as params={"freq": {"unit": "Hz", "min": 0}}'
        if not isinstance(found.get("dangerous", False), bool):
            return {}, "@recipe dangerous should be True or False"
        return found, None
    return {}, None


def _param(arg, default, said: dict | None, source: str) -> tuple[Param, str | None]:
    def written(node) -> str:  # as the file writes it: 1e3, not 1000.0
        return ast.get_source_segment(source, node) or ast.unparse(node)

    annotation = written(arg.annotation) if arg.annotation is not None else ""
    if default is None:
        value, text = _MISSING, ""
    else:
        text = written(default)
        try:
            value = ast.literal_eval(default)
        except _LITERAL_ERRORS:
            value = _UNREAD
    meta = {}
    choices = _literal_choices(arg.annotation)
    if choices is not None:
        meta["choices"] = choices
    said = dict(said or {})
    doc = said.pop("doc", "")
    meta.update(said)
    param = Param(arg.arg, value, text, annotation, doc if isinstance(doc, str) else str(doc))
    try:
        meta = _checked_meta(meta, f"@recipe params {arg.arg}")
    except TypeError as e:
        return param, str(e).replace(", choices", ", choices, doc", 1)
    return dataclasses.replace(param, meta=meta), None


def _literal_choices(annotation) -> list | None:
    """The values of a Literal[...] annotation, if it is one written with literals."""
    if not isinstance(annotation, ast.Subscript):
        return None
    base = annotation.value
    if (base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", None)) != "Literal":
        return None
    items = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    try:
        return [ast.literal_eval(item) for item in items]
    except _LITERAL_ERRORS:
        return None


class _Module:
    """What a recipes file says about devices, read without running it: the names insthub and its
    device() are known by, the files beside it that it imports (any `import name` of a plain name
    may be one: whether that file is there is asked when what runs is put together), the devices
    its top level names, and the facts of each function and of the top level."""

    def __init__(self, tree: ast.Module, path: Path):
        self.path = path
        self.insthub, self.device_funcs = set(), set()
        self.modules = {}  #: name -> the file beside it it would be: `import name`
        self.imported = {}  #: name -> _Ref: `from name import thing`
        for node in ast.walk(tree):  # (imports anywhere: an alias is an alias)
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "insthub" or (a.name.startswith("insthub.") and a.asname is None):
                        self.insthub.add(a.asname or "insthub")
                    elif file := self._beside(a.name):
                        self.modules[a.asname or a.name] = file
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                if node.module in ("insthub", "insthub.client"):
                    self.device_funcs.update(a.asname or a.name for a in node.names if a.name == "device")
                elif file := self._beside(node.module):
                    self.imported.update({a.asname or a.name: _Ref(file, a.name) for a in node.names if a.name != "*"})
        self.functions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.globals, self._later = {}, set()
        top = _top_statements(tree.body)
        for _ in range(2):  # (twice: a name bound from one bound further down)
            for node in (n for statement in top for n in ast.walk(statement)):
                for target, value in _assignments(node):
                    found = self._device_of(value, self._global)
                    if found is _LATER:
                        self._later.add(target)
                    elif found is not None:
                        self.globals[target] = found
        self.top = self._scan(top)
        self.facts = {name: self._scan([node], node) for name, node in self.functions.items()}

    def _beside(self, module: str | None) -> Path | None:
        if not module or "." in module or module == "insthub":
            return None
        file = self.path.parent / f"{module}.py"
        return None if file == self.path else file

    def _global(self, var: str):
        """What a name of the top level is: a device, a _Ref, _LATER, or None."""
        if var in self.globals:
            return self.globals[var]
        return _LATER if var in self._later else self.imported.get(var)

    def device_name(self, node):
        """For insthub.device("name") (or device(...) imported from insthub): the name, or _LATER
        if it is not written out; None for anything else."""
        if not isinstance(node, ast.Call):
            return None
        f = node.func
        if not ((isinstance(f, ast.Attribute) and f.attr == "device" and isinstance(f.value, ast.Name)
                 and f.value.id in self.insthub) or (isinstance(f, ast.Name) and f.id in self.device_funcs)):
            return None
        arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "name"), None)
        return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else _LATER

    def _device_of(self, node, bound):
        """The device an expression is: a name bound to one, insthub.device(...), X.with_timeout(...)
        of a device X, or `module.name` of a file beside it (a _Ref); _LATER, or None."""
        if isinstance(node, ast.Name):
            return bound(node.id)
        name = self.device_name(node)
        if name is not None:
            return name
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "with_timeout":
            owner = node.func.value  # dev.with_timeout(5), or dev.bus.with_timeout(5): the same device
            if isinstance(owner, ast.Attribute) and owner.attr == "bus":
                owner = owner.value
            return self._device_of(owner, bound)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in self.modules \
                and bound(node.value.id) is None:
            return _Ref(self.modules[node.value.id], node.attr)  # import devices; devices.laser
        return None

    def _scan(self, roots: list, fn=None) -> _Facts:
        """The facts of a function (`fn`), or of the top level (its statements, `roots`)."""
        nodes = [n for root in roots for n in ast.walk(root)]
        parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
        facts, local, later_local = _Facts(), {}, set()
        if fn is None:
            bound, shadowed = self._global, set()
        else:  # every name a function assigns is its own (Python's rule), a device or not
            args = fn.args
            shadowed = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                                        *filter(None, [args.vararg, args.kwarg])]}
            shadowed |= {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}

            def bound(var: str):
                if var in local:
                    return local[var]
                if var in later_local:
                    return _LATER
                return None if var in shadowed else self._global(var)

            for _ in range(2):  # (twice: h = g after g = dev.with_timeout(1))
                for node in nodes:
                    for target, value in _assignments(node):
                        found = self._device_of(value, bound)
                        if found is _LATER:
                            later_local.add(target)
                        elif found is not None:
                            local[target] = found
        for node in nodes:
            name = self.device_name(node)
            if name is _LATER:
                facts.later = True
            elif name is not None:
                facts.devices.append((node.lineno, name))
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                found = bound(node.id)
                if found is _LATER:
                    facts.later = True
                elif isinstance(found, (str, _Ref)):
                    facts.devices.append((node.lineno, found))
            if isinstance(node, ast.Attribute) and node.attr != "bus" and not node.attr.startswith("_") \
                    and not isinstance(node.ctx, ast.Del):
                owner = self._device_of(node.value, bound)
                if owner is _LATER:
                    facts.later = True
                elif owner is not None:
                    parent = parents.get(node)
                    if isinstance(node.ctx, ast.Store):
                        kind = "set"
                    elif (isinstance(parent, ast.Call) and parent.func is node) or isinstance(parent, ast.Attribute):
                        kind = "call"  # (dev.cmd.future(...) too)
                    else:
                        kind = "get"
                    facts.uses.append(Use(owner, node.attr, kind, node.lineno))
            if isinstance(node, ast.Call):
                self._named_call(node, bound, facts)
                f = node.func
                if isinstance(f, ast.Name) and f.id not in shadowed:
                    if f.id in self.functions and (fn is None or f.id != fn.name):
                        facts.calls.append(f.id)
                    elif f.id in self.imported:  # from redpitaya import square; square()
                        facts.beside.append(tuple(self.imported[f.id]))
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in self.modules \
                        and f.value.id not in shadowed:  # import redpitaya; redpitaya.square()
                    facts.beside.append((self.modules[f.value.id], f.attr))
            if isinstance(node, ast.Import):
                facts.imports += filter(None, (self._beside(a.name) for a in node.names))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and (file := self._beside(node.module)):
                facts.imports.append(file)
        facts.devices.sort(key=lambda d: d[0])
        facts.uses.sort(key=lambda u: u.line)
        return facts

    def _named_call(self, node: ast.Call, bound, facts: _Facts) -> None:
        """dev.call("fire"), dev.bus.call("fire"), dev.bus.set("power", 3): a use of what the text names."""
        f = node.func
        if not isinstance(f, ast.Attribute):
            return
        if f.attr in _BUS_NAMED and isinstance(f.value, ast.Attribute) and f.value.attr == "bus":
            kind, owner = _BUS_NAMED[f.attr], self._device_of(f.value.value, bound)
        elif f.attr == "call":
            kind, owner = "call", self._device_of(f.value, bound)
        else:
            return
        if owner is _LATER:
            facts.later = True
        elif owner is not None:
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                facts.uses.append(Use(owner, arg.value, kind, node.lineno))
            else:
                facts.later = True


def _top_statements(body: list) -> list:
    """The statements of a module's top level that run when it is imported: not the bodies of its
    functions or methods, nor what `if __name__ == "__main__":` holds (its else does run)."""
    found = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.If) and _is_main(node.test):
            found += _top_statements(node.orelse)
        elif isinstance(node, ast.ClassDef):
            found += _top_statements(node.body)
        else:
            found.append(node)
    return found


def _is_main(test) -> bool:
    """Whether a test is `__name__ == "__main__"` (either way round)."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = (test.left, test.comparators[0])
    return any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides) and \
        any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)


def _assignments(node) -> list[tuple[str, object]]:
    """The (name, value) pairs that `name = value`, `a = b = value`, `name: T = value` and
    `a, b = x, y` bind."""
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = [node.target], node.value
    else:
        return []
    pairs = []
    for target in targets:
        if isinstance(target, ast.Name):
            pairs.append((target.id, value))
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) \
                and len(target.elts) == len(value.elts):
            pairs += [(t.id, v) for t, v in zip(target.elts, value.elts, strict=True) if isinstance(t, ast.Name)]
    return pairs


# ---------------------------------------------------------------- finding
def folder() -> Path:
    """Your recipes ($INSTHUB_RECIPES, default ~/.config/insthub/recipes): every .py file in it whose
    name does not start with _ or a dot."""
    path = os.environ.get(ENV)
    return Path(path).expanduser() if path else Path.home() / ".config" / "insthub" / "recipes"


def your_files() -> list[Path]:
    """The files of your folder that hold recipes, by name."""
    try:
        return sorted(p for p in folder().glob("*.py") if not p.name.startswith(("_", ".")) and p.is_file())
    except OSError:
        return []


def available(discovered: list | None = None) -> tuple[list[Recipe], list[str]]:
    """Every recipe this computer knows of: yours first, then those of each usable extension
    (extensions.discover(), or `discovered`), each with its error if it cannot be used; and what
    keeps recipes from being listed: files that cannot be read, extensions that cannot be used."""
    from . import extensions

    found, problems = [], []
    yours = folder()
    python = extensions.venv_python(yours)
    # As for extensions: nothing that another user may change is run (extensions._others).
    who = (extensions._others(yours) if yours.is_dir() else None) or \
        (extensions._python_shared(Path(python)) if python else None)
    if who:
        problems.append(f"{yours}: insthub runs none of your recipes, since {who}")
    for path in [] if who else your_files():
        if who := extensions._others(path):
            problems.append(f"{path}: insthub runs none of its recipes, since {who}")
            continue
        recipes, said = read_file(path, YOURS, python)
        found += recipes
        problems += said
    for ext in extensions.discover() if discovered is None else discovered:
        if ext.usable:
            for path in ext.recipes:
                recipes, said = read_file(path, ext.id, ext.python)
                found += recipes
                problems += said
        elif ext.recipes and ext.hidden_by is None:
            problems.append(f"extension {ext.id or ext.folder} cannot be used ({ext.error}): its recipes are not "
                            "listed")
    return found, problems


def for_device(name: str, discovered: list | None = None) -> tuple[list[Recipe], list[str]]:
    """The recipes that use the device `name` (as read from their files), and what keeps recipes from
    being listed."""
    found, problems = available(discovered)
    return [r for r in found if name in r.devices], problems


def find(name: str, discovered: list | None = None, *, any_file: bool = True) -> Recipe:
    """The recipe `name`: "square", or "redpitaya.square" (its file's name, then its own), or
    "path/to/file.py:square".  Of several of one name, yours is used; else which one must be
    said.  A path names a recipe of the files available() lists, or, with `any_file` (not for
    agents), of any file.  Raises RecipeError if there is none, or it cannot be used."""
    from . import extensions

    path, sep, function = name.rpartition(":")
    if sep and path.endswith(".py"):
        file = Path(path).expanduser()
        listed, _ = available(discovered)
        found = [r for r in listed if _same_file(r.file, file)]
        if not found and not any_file:
            raise RecipeError(f"{file} is not a file of recipes insthub lists (your folder, extensions'): only those "
                              "run here")
        if not found:
            found, problems = read_file(file, "", extensions.venv_python(file.parent))
            if problems:
                raise RecipeError(problems[0])
        recipe = next((r for r in found if r.name == function), None)
        if recipe is None:
            have = ", ".join(r.name for r in found)
            raise RecipeError(f"{file} has no recipe {function!r}" +
                              (did_you_mean(function, [r.name for r in found]) or f"; it has {have or 'none'}"))
    else:
        found, problems = available(discovered)
        same = [r for r in found if name in (r.name, r.full_name)]
        if not same:
            broken = f" ({len(problems)} cannot be listed: {problems[0]})" if problems else ""
            raise RecipeError(f"no recipe {name!r}" + did_you_mean(name, [r.name for r in found]) + broken)
        mine = [r for r in same if r.origin == YOURS]
        if len(same) > 1 and len(mine) != 1:
            which = mine or same
            raise RecipeError(f"several recipes are called {name!r}: " +
                              ", ".join(f"{runs_as(r, found)} ({r.where()})" for r in which) + "; say which")
        recipe = (mine or same)[0]
    if recipe.error:
        raise RecipeError(f"recipe {recipe.full_name} ({recipe.file}:{recipe.line}) cannot be used: {recipe.error}")
    return recipe


def runs_as(recipe: Recipe, found: list[Recipe]) -> str:
    """The shortest name that find() takes for `recipe`, of the recipes `found` (available()'s): its
    own, its full name, or its file's path with it ("path/lab.py:setup")."""
    for name in (recipe.name, recipe.full_name):
        same = [r for r in found if name in (r.name, r.full_name)]
        mine = [r for r in same if r.origin == YOURS]
        chosen = same[0] if len(same) == 1 else mine[0] if len(mine) == 1 else None
        if chosen is not None and (chosen.file, chosen.name) == (recipe.file, recipe.name):
            return name
    return f"{recipe.file}:{recipe.name}"


def _same_file(a: Path | None, b: Path) -> bool:
    try:
        return a is not None and a.resolve() == b.resolve()
    except OSError:
        return False


# ---------------------------------------------------------------- checking
@dataclasses.dataclass
class Checked:
    """What check() found by asking a recipe's devices: what stops it from running (`problems`), what
    it uses that is marked dangerous (`dangers`: "redpitaya: its command 'sour_set'"), which of its
    devices are simulators (`simulated`), and what could not be checked (`notes`)."""

    problems: list
    dangers: list
    simulated: list
    notes: list


def check(recipe: Recipe, describe=None) -> Checked:
    """Ask each device the recipe uses (`describe(name)` returns its describe(); by default the device
    is found on the bus) whether it is there and not paused, and has the commands and properties
    the recipe uses on it.  Nothing runs.  What a recipe does through code insthub cannot read
    (a name known only when it runs, a module that is not a recipes file beside it) is not
    checked."""
    describe = describe or _describe
    problems, dangers, simulated = [recipe.error] if recipe.error else [], [], []
    described = {}
    for name in dict.fromkeys(recipe.devices + recipe.at_import):
        try:
            d = described[name] = describe(name)
        except DeviceNotFound:
            top = "" if name in recipe.devices else f" (the top of {recipe.file.name} asks for it)"
            problems.append(f"{name} is not on the bus{top}: start its server")
            continue
        except Exception as e:  # noqa: BLE001 - whatever it is, the recipe cannot use the device now
            problems.append(f"{name} did not answer: {type(e).__name__}: {e}")
            continue
        if name not in recipe.devices:  # (only asked for: a proxy of a paused device is made all the same)
            continue
        if d.get("status", {}).get("paused"):
            problems.append(f"{name} is paused: resume it first")
        if d.get("simulated"):
            simulated.append(name)
    for use in recipe.uses:
        d = described.get(use.device)
        if d is None:
            continue
        commands, props, where = d.get("commands", {}), d.get("properties", {}), use.where()
        if use.kind == "set":
            prop = props.get(use.name)
            if prop is None:
                problems.append(f"{where}: {use.device} has no property {use.name!r}" + did_you_mean(use.name, props))
            elif not prop.get("settable"):
                problems.append(f"{where}: {use.device}.{use.name} is read-only")
            elif prop.get("dangerous"):
                dangers.append(f"{use.device}: writing its property {use.name!r}")
        elif use.name in commands:
            if commands[use.name].get("dangerous"):
                dangers.append(f"{use.device}: its command {use.name!r}")
        elif use.name not in props and use.name not in Bus.SHORTCUTS:
            problems.append(f"{where}: {use.device} has no command or property {use.name!r}" +
                            did_you_mean(use.name, [*commands, *props]))
    if recipe.dangerous:
        dangers.insert(0, "the recipe says so (@recipe(dangerous=True))")
    notes = ["it names a device, command or property only when it runs: that was not checked"] \
        if recipe.named_later else []
    return Checked(list(dict.fromkeys(problems)), list(dict.fromkeys(dangers)), simulated, notes)


def _describe(name: str) -> dict:
    from . import client

    return client.device(name, timeout=10.0).bus.describe()  # (fresh: a proxy may be one asked before)


def check_values(recipe: Recipe, values: dict) -> None:
    """Raise RecipeError if `values` names a parameter the recipe does not have, or leaves out one it needs."""
    unknown = [k for k in values if k not in recipe.params]
    if unknown and not recipe.takes_more:
        takes = f"; it takes {', '.join(recipe.params)}" if recipe.params else "; it takes none"
        raise RecipeError(f"recipe {recipe.name} has no parameter {unknown[0]!r}" +
                          (did_you_mean(unknown[0], recipe.params) or takes))
    missing = [p.name for p in recipe.params.values() if p.required and p.name not in values]
    if missing:
        raise RecipeError(f"recipe {recipe.name} needs {' '.join(m + '=...' for m in missing)}")


# ---------------------------------------------------------------- running here
def load(recipe: Recipe | str):
    """The function of a recipe (a Recipe, or a name for find()), from its file as it is now.  This
    runs the file's top level, as importing it would."""
    recipe = recipe if isinstance(recipe, Recipe) else find(recipe)
    return getattr(_import(recipe.file), recipe.name)


def run(recipe: Recipe | str, /, **values):
    """Run a recipe here, in this process, as calling its function would; returns what it returns.
    Nothing is checked first, and nothing asks about what is dangerous.

        from insthub import recipes
        trace = recipes.run("acquire_on_trigger", level=0.2)"""
    recipe = recipe if isinstance(recipe, Recipe) else find(recipe)
    check_values(recipe, values)
    return load(recipe)(**values)


_added: set[str] = set()  #: the folders _import() put on sys.path


def _import(path: Path, first: bool = False):
    """A recipes file, run as a module of a name of its own, from its source as it is now (never
    from bytecode, so that an edit is never missed).  So that it can import the modules beside
    it, its folder goes on sys.path and stays there: first in a recipe's own process (`first`),
    last here, where it never hides one of the caller's modules."""
    import types

    name = f"insthub_recipes_{path.stem}"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    where = str(path.parent)
    if where not in sys.path:
        _added.add(where)
        if first:
            sys.path.insert(0, where)
        else:
            sys.path.append(where)
    sys.modules[name] = module  # (dataclasses in it look their module up there)
    try:
        code = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)  # (not this module's __future__)
        exec(code, module.__dict__)  # noqa: S102 - the recipe is the user's own code, run as they asked
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


# ---------------------------------------------------------------- running in a process of its own
def start(recipe: Recipe, values: dict | None = None, *, capture: bool = True) -> Run:
    """Run a recipe in a process of its own, with its Python (`recipe.python`, else this one), and
    return at once.  With `capture` (the window, agents) what it prints is kept in the Run; without
    it (insthub run) it goes to this terminal.  Raises RecipeError for values it does not take."""
    values = dict(values or {})
    check_values(recipe, values)
    return Run(recipe, values, capture)


class Run:
    """A recipe running in a process of its own (start()): what it prints (`output()`), `stop()`,
    and what it returns (`wait()`)."""

    def __init__(self, recipe: Recipe, values: dict, capture: bool = True):
        import shutil
        import subprocess
        import tempfile
        import weakref

        self.recipe, self.values = recipe, values
        self._dir = Path(tempfile.mkdtemp(prefix="insthub-recipe-"))
        self._remove = weakref.finalize(self, shutil.rmtree, self._dir, True)  # (by wait(), or once forgotten)
        _dump(self._dir / "values", values)
        python = recipe.python or sys.executable
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        if python == sys.executable:  # the same insthub as here, even one that is not installed
            here = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [here, env.get("PYTHONPATH")]))
        piped = subprocess.PIPE if capture else None
        self._proc = subprocess.Popen(
            [python, "-c", _CHILD, str(recipe.file), recipe.name, str(self._dir / "values"), str(self._dir / "result")],
            stdout=piped, stderr=subprocess.STDOUT if capture else None,
            stdin=subprocess.DEVNULL if capture else None, env=env, text=True, encoding="utf-8", errors="replace",
            # captured (the window, agents): only stop() reaches it, not a Ctrl-C meant for the program that started
            # it; else (insthub run) Ctrl-C in the terminal reaches it, as for any program run there
            start_new_session=capture and os.name == "posix")
        self.started = time.monotonic()
        self.ended: float | None = None
        self._out, self._lock, self._stopped, self._reply = [], threading.Lock(), False, None
        self._reader = None
        if capture:
            self._reader = threading.Thread(target=self._collect, name=f"recipe {recipe.name}", daemon=True)
            self._reader.start()

    def _collect(self) -> None:
        kept = 0
        for line in self._proc.stdout:
            with self._lock:
                self._out.append(line)
                kept += len(line)
                while kept > _OUTPUT_KEPT and len(self._out) > 1:
                    kept -= len(self._out.pop(0))
        self._proc.stdout.close()

    def output(self) -> str:
        """What it printed so far (when captured): the last 200 000 characters."""
        with self._lock:
            return "".join(self._out)

    def done(self) -> bool:
        return self._proc.poll() is not None

    @property
    def stopping(self) -> bool:
        """Whether stop() was called and it has not ended yet."""
        return self._stopped and not self.done()

    @property
    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def stop(self, grace: float = 3.0) -> None:
        """End it: first as Ctrl-C would (KeyboardInterrupt, so its `finally` blocks run), then, if it
        is still running after `grace` seconds, for good.  Returns at once; a program that ends
        meanwhile waits for that second step.  Calling it again does nothing: a second interrupt
        would cut the recipe's cleanup short."""
        import subprocess

        if self.done() or self._stopped:
            return
        self._stopped = True
        try:
            self._proc.terminate()
        except OSError:
            return

        def kill():
            try:
                self._proc.wait(grace)
            except subprocess.TimeoutExpired:
                self._proc.kill()

        threading.Thread(target=kill, name="recipe stop").start()  # (not a daemon: see above)

    def wait(self, timeout: float | None = None):
        """Wait until it has ended; return what it returned (also if it returned while being stopped).
        Raises RecipeError if it failed or was stopped (the error says why; its `output`, what it
        printed), and TimeoutError if it is still running after `timeout` seconds (it goes on
        running: stop() it)."""
        import subprocess

        try:
            code = self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"recipe {self.recipe.name} is still running after {timeout:g} s") from None
        if self._reader is not None:
            self._reader.join(5.0)
        if self.ended is None:
            self.ended = time.monotonic()
        if self._reply is None:
            result = self._dir / "result"
            try:
                self._reply = _load(result) if result.exists() else {}
            finally:
                self._remove()
        reply, name, output = self._reply, self.recipe.name, self.output()
        if "error" in reply:
            raise RecipeError(f"recipe {name} failed: {reply['error']}", output)
        if "result" in reply:
            return reply["result"]
        if self._stopped or reply.get("stopped"):
            raise RecipeError(f"recipe {name} was stopped", output)
        raise RecipeError(f"recipe {name} ended without saying how (exit code {code})", output)


def _dump(path: Path, obj) -> None:
    import msgpack

    from . import codec

    body, buffers = codec.encode(obj)
    Path(path).write_bytes(msgpack.packb([body, [memoryview(b).tobytes() for b in buffers]]))


def _load(path: Path):
    import msgpack
    import numpy as np

    from . import codec

    body, buffers = msgpack.unpackb(Path(path).read_bytes())
    return codec.decode(body, [np.frombuffer(bytearray(b), np.uint8) for b in buffers])


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _child(argv: list[str]) -> int:
    """The process of start() (_CHILD): run FILE's FUNCTION with the values in VALUES; write what it
    returned, or how it failed, to RESULT."""
    import signal
    import traceback

    file, function, values_path, result_path = argv
    signal.signal(signal.SIGTERM, _interrupt)  # stop(): as Ctrl-C, so that the recipe's finally blocks run
    try:
        values = _load(Path(values_path))
        result = getattr(_import(Path(file), first=True), function)(**values)
    except KeyboardInterrupt:
        _dump(Path(result_path), {"stopped": True})
        return 130
    except BaseException as e:  # noqa: BLE001 - every failure of the recipe is reported, SystemExit too
        tb = e.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename == __file__:  # from the recipe's own code on
            tb = tb.tb_next
        traceback.print_exception(type(e), e, tb)
        _dump(Path(result_path), {"error": f"{type(e).__name__}: {e}"})
        return 1
    try:
        _dump(Path(result_path), {"result": result})
    except Exception:  # noqa: BLE001 - something the bus cannot carry: its text, then
        _dump(Path(result_path), {"result": repr(result)})
    return 0


# ---------------------------------------------------------------- saving
def save_path(device: str) -> Path:
    """The file of your folder that save() writes a call of `device` into."""
    return folder() / f"{device.replace('.', '_')}.py"


def save(name: str, device: str, command: str, args=(), kwargs: dict | None = None) -> Path:
    """Save one call, `device`.`command`(*args, **kwargs), as the recipe `name`: a function in your
    folder's file for that device (save_path()), replacing the function of that name there (the
    last, which is the one Python uses); the rest of the file stays as it is, a link stays a link.
    Returns the file.  Raises ValueError for a name that cannot be a function's or a parameter's,
    a bad device name, a value Python cannot write out, and a file that is not valid Python."""
    import codecs
    import shutil

    if not name.isidentifier() or keyword.iskeyword(name) or name.startswith("_"):
        raise ValueError(f"{name!r} cannot be a recipe's name: it is a Python function's name, as comb or comb_stop "
                         "(one that starts with _ is private)")
    registry.check_name(device, "device name")
    kwargs = kwargs or {}
    for key in kwargs:
        if not (isinstance(key, str) and key.isidentifier() and not keyword.iskeyword(key)):
            raise ValueError(f"{key!r} cannot be the name of a parameter in Python")
    for value in [*args, *kwargs.values()]:
        try:
            ast.literal_eval(repr(value))
        except _LITERAL_ERRORS:
            raise ValueError(f"{value!r} cannot be written in a recipe as Python") from None
    written = ", ".join([repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()])
    target = f"insthub.device({device!r})"
    if command.isidentifier() and not keyword.iskeyword(command):
        call = f"{target}.{command}({written})"
    else:
        call = f"{target}.bus.call({', '.join([repr(command), *filter(None, [written])])})"
    said = f"{device}.{command}, saved from insthub gui."
    doc = f'"""{said}"""' if said.isprintable() and '"' not in said and "\\" not in said else repr(said)
    function = f"def {name}():\n    {doc}\n    return {call}\n"
    path = save_path(device)
    real = path.resolve()  # (a link to a file kept elsewhere: that file gets the change)
    bom = b""
    if real.exists():
        data = real.read_bytes()
        bom = codecs.BOM_UTF8 if data.startswith(codecs.BOM_UTF8) else b""
        try:
            text = data[len(bom):].decode("utf-8")
            tree = ast.parse(text, str(path))
        except (SyntaxError, UnicodeDecodeError) as e:
            raise ValueError(f"{path} is not valid Python ({e}): fix it first") from None
        lines = text.splitlines(keepends=True)
        old = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if old:
            first = min([d.lineno for d in old[-1].decorator_list] + [old[-1].lineno])
            lines[first - 1:old[-1].end_lineno] = [function]
        else:
            lines = ["".join(lines).rstrip("\n") + "\n\n\n", function]
        if not any(isinstance(n, ast.Import) and any(a.name == "insthub" and a.asname is None for a in n.names)
                   for n in tree.body):
            after = 0  # after the docstring and the __future__ imports
            for n in tree.body:
                if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and n is tree.body[0]) or \
                        (isinstance(n, ast.ImportFrom) and n.module == "__future__"):
                    after = n.end_lineno
                else:
                    break
            lines = "".join(lines).splitlines(keepends=True)
            lines.insert(after, "import insthub\n")
        text = "".join(lines)
    else:
        text = f"{_NEW_FILE}\n\n{function}"
    real.parent.mkdir(parents=True, exist_ok=True)
    temporary = real.with_name(f".{real.name}.tmp")
    temporary.write_bytes(bom + text.encode("utf-8"))
    if real.exists():
        shutil.copymode(real, temporary)
    os.replace(temporary, real)
    _cache.pop(path, None)
    _cache.pop(real, None)
    return path


# ---------------------------------------------------------------- @recipe
def recipe(fn=None, /, *, params: dict | None = None, dangerous: bool = False):
    """Say more about a recipe than its signature does, for the window and for agents:

        from insthub.recipes import recipe

        @recipe(params={"freq": {"unit": "Hz", "min": 0, "doc": "of the square wave"}}, dangerous=True)
        def square(freq: float = 1e3): ...

    `params`: for each parameter, a doc and the metadata a device's property has (unit, min, max,
    step, choices).  `dangerous=True`: ask before running it, as for a command marked dangerous.
    insthub reads it from the file without running it, so its arguments are written out as
    literals.  It changes nothing when the function runs."""
    if fn is not None:
        return fn
    return lambda f: f
