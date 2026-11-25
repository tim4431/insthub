"""Changing a TOML file in place: setting and removing keys of its tables, adding,
removing and renaming tables, and leaving everything else as it was: comments,
layout, line endings.

Python reads TOML (`tomllib`) but cannot write it, and writing a file again from
what was read would lose its comments and its layout.  So this edits the text,
one statement at a time:

    edit(text, {("devices", "psu"): {"lazy": True, "port": None},  # set one key, remove another
                ("devices", "old"): None,                           # remove a table (and its sub-tables)
                ("server",): {"name": "lab"}},                      # a table that is missing is added
         renames={("devices", "box"): ("devices", "psu2")})         # renamed where it is, comments kept

A changed value keeps its line and the comment after it (a value that spanned
several lines becomes one line, without the comments inside it); a new key goes
after the last key of its table; a new table goes at the end.  The result is
then read back, and if it does not say exactly what was asked (the same values,
of the same types; the table written inline or with dotted keys, say),
ValueError is raised instead of guessing.  read() and write() read and write
such files as UTF-8, write() in one step, keeping a symbolic link and the
file's permissions and group, and refusing a file that is read-only.

`value()` and `key()` write a single value or key; config.dump uses them.
Only the standard library is used: registry.py edits devices.toml with this.
"""
from __future__ import annotations

import copy
import datetime
import errno
import json
import math
import numbers
import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path

_BARE = re.compile(r"[A-Za-z0-9_-]+")


# ---------------------------------------------------------------- writing values
def key(name: str) -> str:
    """A key as written in TOML: bare if it can be, else quoted."""
    return name if _BARE.fullmatch(name) else value(name)


def value(v) -> str:
    """A value as written in TOML: strings, numbers, booleans, dates and times, and lists and
    dicts of them (numpy numbers too)."""
    if isinstance(v, bool) or (getattr(v, "dtype", None) is not None and v.dtype.kind == "b"):
        return "true" if v else "false"
    if isinstance(v, numbers.Integral):
        return str(int(v))
    if isinstance(v, numbers.Real):
        f = float(v)
        if math.isnan(f):
            return "nan"
        return ("inf" if f > 0 else "-inf") if math.isinf(f) else repr(f)
    if isinstance(v, str):  # a JSON string is a TOML basic string, but for what TOML wants escaped besides
        return (json.dumps(v, ensure_ascii=False).replace("\x7f", "\\u007f").replace("\u2028", "\\u2028")
                .replace("\u2029", "\\u2029").replace("\x85", "\\u0085"))
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{key(str(k))} = {value(x)}" for k, x in v.items()) + " }" if v else "{}"
    raise ValueError(f"{v!r} cannot be written to a TOML file (it has no {type(v).__name__})")


def header(path: tuple) -> str:
    return "[" + ".".join(key(p) for p in path) + "]"


def same(a, b) -> bool:
    """Equal, and of the same kind: true is not 1, 1 is not 1.0, nan is nan, a tuple is a list."""
    if isinstance(a, dict) or isinstance(b, dict):
        return (isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys()
                and all(same(a[k], b[k]) for k in a))
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        return (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)) and len(a) == len(b)
                and all(same(x, y) for x, y in zip(a, b, strict=True)))
    kind = _kind(a)
    if kind is not _kind(b):
        return False
    if kind is float:
        a, b = float(a), float(b)
        return (math.isnan(a) and math.isnan(b)) or (a == b and math.copysign(1, a) == math.copysign(1, b))
    return a == b


def _kind(x) -> type:
    if isinstance(x, bool) or getattr(getattr(x, "dtype", None), "kind", None) == "b":
        return bool
    if isinstance(x, numbers.Integral):
        return int
    if isinstance(x, numbers.Real):
        return float
    return str if isinstance(x, str) else type(x)


# ---------------------------------------------------------------- reading the text as statements
class _Statement:
    """Some lines of the file: a table header, an array-of-tables header, a key = value entry
    (one or more lines), or "other" (a blank line or a comment).  `path`: the table's, or the
    entry's key."""

    __slots__ = ("ends", "kind", "lines", "path")

    def __init__(self, kind: str, lines: list[str], path: tuple = (), ends: list | None = None):
        self.kind, self.lines, self.path = kind, lines, path
        self.ends = ends if ends is not None else [None] * len(lines)  #: each line's ending; None: the file's

    @property
    def blank(self) -> bool:
        return self.kind == "other" and not self.lines[0].strip()


def _walk(d) -> tuple:
    path = []
    while isinstance(d, dict) and len(d) == 1:
        (k, d), = d.items()
        path.append(k)
    return tuple(path)


def _equals_at(line: str) -> int:
    """Where the `=` of an entry is (not one inside a quoted key)."""
    quote, i = None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "=":
            return i
        i += 1
    raise ValueError(f"not a key = value line: {line.strip()!r}")


def _key_path(line: str) -> tuple:
    d = tomllib.loads(line[:_equals_at(line)] + "= 0")
    path = []
    while isinstance(d, dict):
        (k, d), = d.items()
        path.append(k)
    return tuple(path)


def _entry_end(lines: list[str], i: int) -> int:
    """The index after the last line of the entry that starts at lines[i]: brackets and
    multi-line strings carry it over (in one pass: a long list stays cheap)."""
    depth, mode, j = 0, None, i
    while j < len(lines):
        line, k = lines[j], 0
        while k < len(line):
            ch = line[k]
            if mode in ('"""', "'''"):
                if line.startswith(mode, k):
                    run = len(line[k:]) - len(line[k:].lstrip(mode[0]))  # a closing """ may follow one or two quotes
                    k += max(3, run)
                    mode = None
                elif mode == '"""' and ch == "\\":
                    k += 2
                else:
                    k += 1
            elif mode:
                if mode == '"' and ch == "\\":
                    k += 2
                    continue
                if ch == mode:
                    mode = None
                k += 1
            elif ch == "#":
                break
            elif line.startswith('"""', k) or line.startswith("'''", k):
                mode, k = line[k:k + 3], k + 3
            else:
                if ch in "\"'":
                    mode = ch
                elif ch in "[{":
                    depth += 1
                elif ch in "]}":
                    depth -= 1
                k += 1
        if mode in ('"', "'"):
            mode = None  # a one-line string never goes on (the file would not have parsed)
        j += 1
        if depth <= 0 and mode is None:
            return j
    return j


def _statements(lines: list[str], ends: list | None = None) -> list[_Statement]:
    ends = ends if ends is not None else [None] * len(lines)
    out, i = [], 0
    while i < len(lines):
        bare = lines[i].strip()
        if not bare or bare.startswith("#"):
            out.append(_Statement("other", [lines[i]], ends=ends[i:i + 1]))
            i += 1
        elif bare.startswith("[["):  # an array of tables: kept as it is, never edited
            out.append(_Statement("array", [lines[i]], _walk(tomllib.loads(lines[i])), ends[i:i + 1]))
            i += 1
        elif bare.startswith("["):
            out.append(_Statement("table", [lines[i]], _walk(tomllib.loads(lines[i])), ends[i:i + 1]))
            i += 1
        else:  # an entry: as many lines as it takes to be a whole statement (a long list, a long string)
            j = _entry_end(lines, i)
            try:
                tomllib.loads("\n".join(lines[i:j]))
            except tomllib.TOMLDecodeError:
                j = i + 1  # the scan was wrong: find the end by reading, line by line
                while True:
                    try:
                        tomllib.loads("\n".join(lines[i:j]))
                        break
                    except tomllib.TOMLDecodeError:
                        if j >= len(lines):
                            raise ValueError(f"line {i + 1}: cannot read {bare!r}") from None
                        j += 1
            out.append(_Statement("entry", lines[i:j], _key_path(lines[i]), ends[i:j]))
            i = j
    return out


def _trailing_comment(lines: list[str]) -> str:
    """The comment after the end of a statement (with the spaces before it), or ""."""
    whole = tomllib.loads("\n".join(lines))
    last = lines[-1]
    for i, ch in enumerate(last):
        if ch == "#":
            try:
                if same(tomllib.loads("\n".join([*lines[:-1], last[:i]])), whole):
                    return last[len(last[:i].rstrip()):]
            except tomllib.TOMLDecodeError:
                continue
    return ""


def _rewrite(statement: _Statement, v) -> None:
    """Give an entry a new value, keeping its key as written and a comment after it."""
    first = statement.lines[0]
    at = _equals_at(first)
    prefix = first[:at + 1] + re.match(r"\s*", first[at + 1:]).group(0)
    statement.lines = [prefix + value(v) + _trailing_comment(statement.lines)]
    statement.ends = statement.ends[-1:]


def _rename(statement: _Statement, path: tuple) -> None:
    """Give a table header another path, keeping a comment after it and the indentation."""
    line = statement.lines[0]
    indent = line[:len(line) - len(line.lstrip())]
    statement.lines = [indent + header(path) + _trailing_comment([line.strip(" \t")])]
    statement.path = path


def _section(statements: list[_Statement], path: tuple) -> tuple[int, int] | None:
    """(index of the table's header, index after its last statement), or None if it has no header.
    The top of the file, before any header, is the table ()."""
    if path:
        found = next((i for i, s in enumerate(statements) if s.kind == "table" and s.path == path), None)
        if found is None:
            return None
        start = found
    else:
        start = -1
    end = next((i for i in range(start + 1, len(statements)) if statements[i].kind in ("table", "array")),
               len(statements))
    return start, end


def _last_entry(statements: list[_Statement], start: int, end: int) -> int:
    """Where a new entry of a section goes: after its last entry, before any comments that follow."""
    last = start
    for i in range(start + 1, end):
        if statements[i].kind == "entry":
            last = i
    return last + 1


def _remove_section(statements: list[_Statement], start: int, end: int) -> None:
    """Remove a table: its header, its entries and the comments after them (but for a comment
    block just above the next header, which belongs to that table), and the comment block
    above its header if that block stands on its own (after a blank line, not at the top)."""
    stop = end
    while stop > start + 1 and statements[stop - 1].kind == "other" and not statements[stop - 1].blank \
            and stop < len(statements):
        stop -= 1  # the next table's own comments
    above = start
    while above > 0 and statements[above - 1].kind == "other" and not statements[above - 1].blank:
        above -= 1
    if above < start and above > 0 and statements[above - 1].blank:
        start = above
    del statements[start:stop]
    if 0 < start < len(statements):
        if statements[start - 1].blank and statements[start].blank:
            del statements[start]  # no two blank lines where the table was
        elif not statements[start - 1].blank and not statements[start].blank:
            statements.insert(start, _Statement("other", [""]))  # but one between the tables around it
    if start == 0:
        while statements and statements[0].blank:
            del statements[0]
    elif start >= len(statements):
        while statements and statements[-1].blank:
            del statements[-1]


def _lookup(doc: dict, path: tuple):
    for p in path:
        if not isinstance(doc, dict) or p not in doc:
            return None
        doc = doc[p]
    return doc


def _plain(v):
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _prune(d):
    """Without empty tables (one left behind by a removal reads back as not there)."""
    if not isinstance(d, dict):
        return d
    out = {k: _prune(v) for k, v in d.items()}
    return {k: v for k, v in out.items() if v != {} or not isinstance(d[k], dict)}


def _expected(old: dict, changes: dict, renames: dict) -> dict:
    doc = copy.deepcopy(old)
    for a in renames:
        for b in renames:
            if a != b and b[:len(a)] == a:
                raise ValueError(f"[{'.'.join(a)}] and its [{'.'.join(b)}] cannot be renamed at once")
    if any(not path for pair in renames.items() for path in pair):
        raise ValueError("a rename needs the path of a table, not of the top of the file")
    moved = {}
    for path in renames:  # all taken out first, then all put back: two tables may swap names
        parent = _lookup(doc, path[:-1]) if path else None
        if not isinstance(parent, dict) or path[-1] not in parent:
            raise ValueError(f"[{'.'.join(path)}] is not in the file")
        moved[path] = parent.pop(path[-1])
    for path, new in sorted(renames.items(), key=lambda pn: len(pn[1])):  # (d before d.e)
        table = doc
        for p in new[:-1]:
            table = table.setdefault(p, {})
            if not isinstance(table, dict):
                raise ValueError(f"[{'.'.join(new)}] cannot be made in this file")  # noqa: TRY004
        if new[-1] in table:
            raise ValueError(f"[{'.'.join(new)}] is in the file already")
        table[new[-1]] = moved[path]
    for path, keys in changes.items():
        if keys is None:
            parent = _lookup(doc, path[:-1]) if path else None
            if isinstance(parent, dict):
                parent.pop(path[-1], None)
            continue
        table = doc
        for p in path:
            table = table.setdefault(p, {})
            if not isinstance(table, dict):  # under an array of tables, or a value of its own
                raise ValueError(  # noqa: TRY004 (what callers catch for a request that cannot be done)
                    f"[{'.'.join(path)}] is not a table this can change; edit the file by hand")
        for k, v in keys.items():
            if v is None:
                table.pop(k, None)
            else:
                table[k] = _plain(v)
    return doc


def _split(text: str) -> tuple[list[str], list[str]]:
    """(the lines, the ending of each: "\\n", "\\r\\n", or "" for a last line without one).  Only a
    line feed ends a line (TOML allows U+2028 and the like in strings)."""
    lines, ends = [], []
    parts = text.split("\n")
    for part in parts[:-1]:
        crlf = part.endswith("\r")
        lines.append(part[:-1] if crlf else part)
        ends.append("\r\n" if crlf else "\n")
    if parts[-1]:
        lines.append(parts[-1])
        ends.append("")
    return lines, ends


# ---------------------------------------------------------------- editing
def tables(text: str) -> list[tuple]:
    """The paths of the tables that have a header of their own ([devices.psu] -> ("devices", "psu"))."""
    return [s.path for s in _statements(*_split(text)) if s.kind == "table"]


def edit(text: str, changes: dict, renames: dict | None = None) -> str:
    """The text with these changes: {table path: {key: value, or None to remove it}, or None
    to remove the table and its sub-tables}, after renaming the tables in `renames`
    ({old path: new path}, sub-tables with them, all at once: two may swap names).  A path in
    `changes` names a table as it is after the renames (an old name is then a new table).  Raises ValueError if the result would not
    say exactly what was asked."""
    old = tomllib.loads(text)  # TOMLDecodeError (a ValueError) for a file that is not TOML
    renames = {tuple(k): tuple(v) for k, v in (renames or {}).items()}
    changes = {tuple(k): v for k, v in changes.items()}
    expected = _prune(_expected(old, changes, renames))  # what it must say (ValueError for what cannot be asked)
    lines, ends = _split(text)
    statements = _statements(lines, ends)
    crlf = ends.count("\r\n")
    newline = "\r\n" if crlf > len(ends) - crlf - (ends[-1:] == [""]) else "\n"  # for new lines: the file's usual
    final = not ends or ends[-1] != ""  # it ended with a line ending (a new file gets one)

    def before(path: tuple) -> tuple | None:  # where a table was before the renames (None: a name renamed away)
        into = max(((o, n) for o, n in renames.items() if path[:len(n)] == n), key=lambda on: len(on[1]),
                   default=None)  # (the longest: x->d and y->d.e put d.e's tables under y)
        moved = None if into is None else into[0] + path[len(into[1]):]
        if moved is None and any(path[:len(o)] == o for o in renames):
            return None  # (a change there makes a new table: the one of that name has another name now)
        return path if moved is None else moved

    def was(path: tuple):
        where = before(path)
        return None if where is None else _lookup(old, where)

    for s in statements:  # every header from its path as it was: two tables may swap names
        if s.kind == "table":
            for path, new in renames.items():
                if s.path[:len(path)] == path:
                    _rename(s, new + s.path[len(path):])
                    break
    removed: list[tuple] = []  # tables removed so far in this call: what is asked under them is new
    for path, keys in changes.items():
        found = _section(statements, path)
        if keys is None:  # remove the table, and its sub-tables
            removed.append(path)
            subs = [i for i, s in enumerate(statements) if s.kind == "table" and len(s.path) > len(path)
                    and s.path[:len(path)] == path]
            if found is None and not subs:
                if any(p != path and path[:len(p)] == p and changes[p] is None for p in changes):
                    continue  # removed with a table it is in
                if path and was(path) is not None:
                    raise ValueError(f"[{'.'.join(path)}] is not written as a table of its own; edit it by hand")
                continue
            for start in sorted(subs + ([found[0]] if found else []), reverse=True):
                _remove_section(statements, start, _section(statements, statements[start].path)[1])
            continue
        if found is None:  # a new table, at the end
            existing = None if any(path[:len(r)] == r for r in removed) else was(path)
            if existing is not None and not (isinstance(existing, dict) and
                                             all(isinstance(v, dict) for v in existing.values())):
                raise ValueError(f"[{'.'.join(path)}] is written inline or with dotted keys; edit it by hand")
            if statements and not statements[-1].blank:
                statements.append(_Statement("other", [""]))
            statements.append(_Statement("table", [header(path)], path))
            statements += [_Statement("entry", [f"{key(k)} = {value(v)}"], (k,)) for k, v in keys.items()
                           if v is not None]
            continue
        start, end = found
        old_table = {} if any(path[:len(r)] == r for r in removed) else was(path) or {}
        for k, v in keys.items():
            at = next((i for i in range(start + 1, end) if statements[i].kind == "entry" and statements[i].path == (k,)),
                      None)
            if v is None:
                if at is not None:
                    del statements[at]
                    end -= 1
                    if 0 < at < len(statements) and statements[at - 1].blank and statements[at].blank:
                        del statements[at]  # no two blank lines where the key was
                        end -= 1
                    while at == len(statements) and statements and statements[-1].blank:
                        del statements[-1]  # nor one at the end of the file, where the last key was
                        at -= 1
                        end -= 1
                    while at == 0 and statements and statements[0].blank:
                        del statements[0]  # nor at its top
                        end -= 1
            elif at is not None:
                if not same(old_table.get(k), v):
                    _rewrite(statements[at], v)
            else:
                where = _last_entry(statements, start, end)
                if where == 0:  # the first key of the top of the file: after its opening comments
                    while where < end and statements[where].kind == "other" and not statements[where].blank:
                        where += 1
                statements.insert(where, _Statement("entry", [f"{key(k)} = {value(v)}"], (k,)))
                end += 1
    out = [(line, end) for s in statements for line, end in zip(s.lines, s.ends, strict=True)]
    new = "".join(line + (end or newline) for line, end in out[:-1])
    if out:
        line, end = out[-1]
        new += line + ((end or newline) if final else "")
    try:
        result = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        result = None
    if result is None or not same(_prune(result), expected):
        raise ValueError("the file is written in a way this cannot change safely; edit it by hand")
    return new


# ---------------------------------------------------------------- files
def read(path: str | Path) -> str:
    """A TOML file's text, as UTF-8 (as tomllib reads it), with its own line endings."""
    return Path(path).expanduser().read_bytes().decode("utf-8")


def write(path: str | Path, text: str) -> None:
    """Replace a file in one step (a reader never sees half of it), as UTF-8.  A symbolic
    link stays one (its target is written), and the file keeps its permissions and group.
    A file that is read-only for this user is not replaced (PermissionError), nor one in a
    folder this user cannot write (the new text is written next to it, then moved over it).
    A second name of the file (a hard link) keeps the old text."""
    path = Path(path).expanduser()
    real = _target(path)
    data = text.encode("utf-8")  # (before anything is written)
    old = real.stat() if real.exists() else None
    if old is not None and not os.access(real, os.W_OK):
        raise PermissionError(errno.EACCES, "the file is read-only", str(real))
    real.parent.mkdir(parents=True, exist_ok=True)
    try:  # a new file of its own (never one somebody put there under the name, nor a link), private at first
        fd, name = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".tmp")
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        why = "it is on a read-only file system" if e.errno == errno.EROFS else \
            "its folder is read-only for you (the file is replaced in one step, through a new file next to it)"
        raise PermissionError(e.errno, why, str(real)) from None
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            mode = stat.S_IMODE(old.st_mode) if old is not None else 0o666 & ~_umask()
            if hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)
            else:  # (Windows)
                os.chmod(tmp, mode)
            if old is not None and hasattr(os, "fchown"):
                try:  # a file shared through its group stays shared (the owner: only root can keep it)
                    os.fchown(f.fileno(), old.st_uid if os.geteuid() == 0 else -1, old.st_gid)
                except OSError:
                    pass
        os.replace(tmp, real)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _target(path: Path) -> Path:
    """The file a path names, through symbolic links, but only links of this user's or root's (as
    the kernel's protected_symlinks has it for shared sticky folders): one somebody else put in a
    shared folder would have this user overwrite a file of their choosing (PermissionError)."""
    seen = 0
    while path.is_symlink():
        owner = os.lstat(path).st_uid
        if hasattr(os, "geteuid") and owner not in (os.geteuid(), 0):
            raise PermissionError(errno.EACCES, "it is a symbolic link made by another user; not followed",
                                  str(path))
        seen += 1
        if seen > 40:
            raise OSError(errno.ELOOP, "too many levels of symbolic links", str(path))
        path = (path.parent / os.readlink(path)).absolute()
    return path


def _umask() -> int:
    """The process's umask, for the mode of a new file (as open() would give it)."""
    try:  # Linux: read it, without setting it (which would race with other threads)
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Umask:"):
                return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def update(path: str | Path, changes: dict, new_file: str = "", renames: dict | None = None) -> str:
    """Edit a file in place (made from `new_file` if it does not exist yet); returns its new text."""
    path = Path(path).expanduser()
    text = edit(read(path) if path.exists() else new_file, changes, renames)
    write(path, text)
    return text
