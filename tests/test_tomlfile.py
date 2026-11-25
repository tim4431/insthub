"""Editing TOML files in place: what changes is only what was asked."""
import tomllib

import pytest

from insthub import tomlfile

CONFIG = '''\
# The server of the analyzer.  Start it with:  insthub serve hp8562a.toml

[server]
name = "hp8562a"

# the analyzer itself
[devices.hp8562a]
driver = "bus_server:SpectrumAnalyzer"
path = "~/Code/hp_8562a_gpib"
board = 0                      # the GPIB card
pad = 18
gains = [
    1, 2,   # coarse
    5,
]
note = """
[not a table]
"""

[devices.spare]                # an old one
driver = "x:Y"
'''


def test_a_changed_value_keeps_its_line_and_its_comment():
    new = tomlfile.edit(CONFIG, {("devices", "hp8562a"): {"board": 1, "pad": 18, "lazy": True}})
    assert "board = 1                      # the GPIB card\n" in new
    assert "pad = 18\n" in new                                            # unchanged: left as it was
    assert new.replace("board = 1 ", "board = 0 ").replace('"""\nlazy = true\n', '"""\n') == CONFIG
    assert tomllib.loads(new)["devices"]["hp8562a"]["lazy"] is True       # new keys after the last key


def test_multiline_values_are_one_statement():
    new = tomlfile.edit(CONFIG, {("devices", "hp8562a"): {"gains": [3], "note": None}})
    doc = tomllib.loads(new)["devices"]["hp8562a"]
    assert doc["gains"] == [3] and "note" not in doc and "[not a table]" not in new
    assert "gains = [3]\n" in new and "# coarse" not in new


def test_tables_are_added_and_removed():
    new = tomlfile.edit(CONFIG, {("devices", "spare"): None, ("devices", "sa"): {"driver": "a:B", "lazy": None},
                                 ("server",): {"tcp": "0.0.0.0:8700"}})
    assert "spare" not in new and "# an old one" not in new
    assert new.endswith('\n\n[devices.sa]\ndriver = "a:B"\n')
    assert tomllib.loads(new)["server"] == {"name": "hp8562a", "tcp": "0.0.0.0:8700"}
    assert "# the analyzer itself\n[devices.hp8562a]" in new             # the comment above a table stays with it
    gone = tomlfile.edit(CONFIG, {("devices", "hp8562a"): None})
    assert "# the analyzer itself" not in gone and "board" not in gone and "[devices.spare]" in gone
    assert "\n\n\n" not in gone


def test_quoted_keys_and_odd_values():
    text = '[devices."odd.name"]\n"two words" = 1\nport = \'C:\\dev #1\'  # a port\n'
    new = tomlfile.edit(text, {("devices", "odd.name"): {"two words": 2, "port": 'C:\\dev #2 "x"', "new key": [1.5]}})
    assert tomllib.loads(new) == {"devices": {"odd.name": {"two words": 2, "port": 'C:\\dev #2 "x"',
                                                           "new key": [1.5]}}}
    assert new.splitlines()[2].endswith("  # a port") and new.splitlines()[3] == '"new key" = [1.5]'


def test_what_it_cannot_edit_safely_is_refused():
    with pytest.raises(ValueError, match="by hand"):
        tomlfile.edit('devices = { awg = "tcp://h:1" }\n', {("devices",): {"psu": "tcp://h:2"}})
    with pytest.raises(ValueError, match="by hand"):
        tomlfile.edit('[devices]\npsu.port = "x"\n', {("devices", "psu"): {"port": "y"}})
    with pytest.raises(ValueError):
        tomlfile.edit("this is not = toml = at all\n", {("a",): {"b": 1}})
    with pytest.raises(ValueError, match="cannot be written"):
        tomlfile.edit("", {("a",): {"b": object()}})


def test_a_super_table_may_follow_its_sub_tables():
    new = tomlfile.edit("[devices.psu]\nx = 1\n", {("devices",): {"awg": "tcp://h:1"}})
    assert tomllib.loads(new) == {"devices": {"psu": {"x": 1}, "awg": "tcp://h:1"}}


def test_update_writes_the_file_and_makes_a_new_one(tmp_path):
    path = tmp_path / "sub" / "lab.toml"
    text = tomlfile.update(path, {("server",): {"name": "lab"}}, new_file="# made here\n")
    assert path.read_text() == text == '# made here\n\n[server]\nname = "lab"\n'
    tomlfile.update(path, {("server",): {"tcp": 8700}})
    assert tomllib.loads(path.read_text()) == {"server": {"name": "lab", "tcp": 8700}}
    assert list(path.parent.iterdir()) == [path]                          # no temporary file left behind


def test_types_are_kept_and_compared_strictly():
    assert tomllib.loads(tomlfile.edit("[a]\nx = 1\n", {("a",): {"x": True}}))["a"]["x"] is True
    assert tomllib.loads(tomlfile.edit("[a]\nx = 1\n", {("a",): {"x": 1.0}}))["a"]["x"] == 1.0
    assert type(tomllib.loads(tomlfile.edit("[a]\nx = 1\n", {("a",): {"x": 1.0}}))["a"]["x"]) is float
    assert tomlfile.edit("[a]\nx = 1   # one\n", {("a",): {"x": 1}}) == "[a]\nx = 1   # one\n"    # the same: untouched
    text = "[a]\nx = nan\n[b]\ny = 1\n"                                   # a nan elsewhere does not block edits
    assert tomllib.loads(tomlfile.edit(text, {("b",): {"y": 2, "t": (1, 2)}}))["b"] == {"y": 2, "t": [1, 2]}
    assert tomlfile.same(float("nan"), float("nan")) and not tomlfile.same(1, True) and not tomlfile.same(1, 1.0)


def test_line_endings_unicode_and_odd_keys_survive():
    crlf = "# c\r\n[a]\r\nx = 1\r\ny = 2\r\n"
    assert tomlfile.edit(crlf, {("a",): {"x": 5}}) == "# c\r\n[a]\r\nx = 5\r\ny = 2\r\n"
    assert tomlfile.edit("[a]\nx = 1", {}) == "[a]\nx = 1"                # no final newline: none added
    text = '[a]\nnote = "line sep"  #   too\n"q\\"" = 1\n'
    new = tomlfile.edit(text, {("a",): {"x": "a b"}})
    assert new.startswith(text) and tomlfile.edit(new, {("a",): {"y": 1}})   # its own output stays editable
    import datetime
    when = datetime.datetime(2024, 1, 1, 7, 30)
    assert tomllib.loads(tomlfile.edit("", {("a",): {"t": when, "d": when.date()}}))["a"] == {"t": when,
                                                                                            "d": when.date()}


def test_sub_tables_renames_and_comments_around_removed_tables():
    text = ("# my lab: keep this\n[devices.psu]\ndriver = \"a:B\"  # the supply\n\n[devices.psu.kwargs]\npath = 1\n"
            "\n# the generator\n[devices.awg]\ndriver = \"c:D\"\n# lazy = true   (an option of awg)\n")
    gone = tomlfile.edit(text, {("devices", "psu"): None})
    assert gone == '# my lab: keep this\n\n# the generator\n[devices.awg]\ndriver = "c:D"\n# lazy = true   (an option of awg)\n'
    gone = tomlfile.edit(text, {("devices", "awg"): None})
    assert "# the generator" not in gone and gone.endswith("path = 1\n") and "\n\n\n" not in gone
    moved = tomlfile.edit(text, {("devices", "box"): {"lazy": True}}, renames={("devices", "psu"): ("devices", "box")})
    assert '[devices.box]\ndriver = "a:B"  # the supply\nlazy = true\n\n[devices.box.kwargs]' in moved
    assert tomllib.loads(moved)["devices"]["box"]["kwargs"] == {"path": 1} and "psu" not in moved
    assert tomlfile.edit('[a]\ng = [\n  1,  # coarse\n  2,\n]  # gains, in dB\n', {("a",): {"g": [3]}}) == \
        "[a]\ng = [3]  # gains, in dB\n"
    assert tomlfile.edit("[a]\nx = 1\n\ny = 2\n\nz = 3\n", {("a",): {"y": None}}) == "[a]\nx = 1\n\nz = 3\n"
    with pytest.raises(ValueError, match="by hand"):
        tomlfile.edit("[[x]]\nn = 1\n[x.sub]\nk = 1\n", {("x", "sub"): {"k": 2}})


def test_a_long_list_is_read_in_one_pass():
    import time
    text = "[a]\nvalues = [\n" + "".join(f"    [{i}, {i * 0.5}],\n" for i in range(4000)) + "]\nx = 1\n"
    start = time.perf_counter()
    assert tomllib.loads(tomlfile.edit(text, {("a",): {"x": 2}}))["a"]["x"] == 2
    assert time.perf_counter() - start < 1.0


def test_files_keep_their_link_permissions_and_encoding(tmp_path):
    import os
    real = tmp_path / "real.toml"
    real.write_bytes(b'[a]\nx = 1\r\n')
    real.chmod(0o640)
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    tomlfile.update(link, {("a",): {"unit": "µA Ω"}})
    assert link.is_symlink() and tomllib.loads(real.read_bytes().decode("utf-8"))["a"]["unit"] == "µA Ω"
    assert oct(os.stat(real).st_mode & 0o777) == "0o640" and sorted(p.name for p in tmp_path.iterdir()) == \
        ["link.toml", "real.toml"]


def test_triple_quoted_strings_in_a_line_do_not_swallow_what_follows():
    text = '[a]\nx = ["""p""", 1]\n# keep me: what x means\n\n# and me\n'
    assert tomlfile.edit(text, {("a",): {"x": [2]}}) == '[a]\nx = [2]\n# keep me: what x means\n\n# and me\n'
    config = ("[devices.psu]\ndriver = 'a:B'\nargs = ['''C:\\data\\cal.csv''', 3]   # calibration file\n"
              "# lazy = true\n\n[devices.awg]\ndriver = 'c:D'\n")
    assert "# lazy = true" in tomlfile.edit(config, {("devices", "psu"): {"args": ["x", 3]}})
    assert "driver = \"e:F\"" in tomlfile.edit(config, {("devices", "awg"): {"driver": "e:F"}})
    assert "awg" not in tomlfile.edit(config, {("devices", "awg"): None})
    hint = '[s]\nidn = """SIM,SCOPE"""   # answers: [model, serial\nport = 1\n'
    assert tomlfile.edit(hint, {("s",): {"port": 2}}) == '[s]\nidn = """SIM,SCOPE"""   # answers: [model, serial\nport = 2\n'
    inline = "[a]\nx = { s = '''p''', t = 1 }   # what x is\n# more\n[b]\ny = 1\n"
    assert tomlfile.edit(inline, {("b",): {"y": 2}}).endswith("# more\n[b]\ny = 2\n")


def test_a_comment_after_nan_is_kept():
    assert tomlfile.edit("[a]\nnoise = nan   # V rms\n", {("a",): {"noise": 0.1}}) == "[a]\nnoise = 0.1   # V rms\n"
    assert tomlfile.edit("[a]\ng = [1, nan]   # gains\n", {("a",): {"g": [1, 2]}}) == "[a]\ng = [1, 2]   # gains\n"


def test_each_line_keeps_its_own_ending():
    mixed = "# c\n[a]\r\nx = 1\n"
    assert tomlfile.edit(mixed, {}) == mixed                                    # nothing asked: nothing changed
    assert tomlfile.edit(mixed, {("a",): {"x": 2}}) == "# c\n[a]\r\nx = 2\n"
    assert tomlfile.edit("[a]\r\nx = 1\r\n", {("a",): {"y": 2}}) == "[a]\r\nx = 1\r\ny = 2\r\n"  # new: the usual one
    assert tomlfile.edit("[a]\nx = 1", {("a",): {"y": 2}}) == "[a]\nx = 1\ny = 2"           # still no last newline


def test_renames_happen_at_once_and_small_things():
    text = "[d.psu]   # the supply\nx = 1\n\n[d.awg]\ny = 2\n"
    swapped = tomlfile.edit(text, {}, renames={("d", "psu"): ("d", "awg"), ("d", "awg"): ("d", "psu")})
    assert swapped == "[d.awg]   # the supply\nx = 1\n\n[d.psu]\ny = 2\n"
    with pytest.raises(ValueError, match="in the file already"):            # onto a table that stays
        tomlfile.edit(text, {}, renames={("d", "psu"): ("d", "awg")})
    assert tomlfile.edit("[a]  # note \u2028\nx = 1\n", {}, renames={("a",): ("b",)}) == "[b]  # note \u2028\nx = 1\n"
    assert tomlfile.edit("[a]\nx = 0.0\n", {("a",): {"x": -0.0}}) == "[a]\nx = -0.0\n"
    import enum

    import numpy as np

    class Color(enum.StrEnum):
        RED = "red"
    assert 'c = "red"' in tomlfile.edit("[a]\n", {("a",): {"c": Color.RED, "on": np.bool_(True)}})
    assert tomlfile.edit("[a]\nx = 1\n", {("a",): None}) == ""


def test_a_read_only_file_is_not_replaced(tmp_path):
    import os
    path = tmp_path / "ro.toml"
    path.write_text("[a]\nx = 1\n")
    path.chmod(0o444)
    if os.access(path, os.W_OK):
        pytest.skip("root may write anything")
    with pytest.raises(PermissionError):
        tomlfile.update(path, {("a",): {"x": 2}})
    assert path.read_text() == "[a]\nx = 1\n" and [p.name for p in tmp_path.iterdir()] == ["ro.toml"]


def test_write_makes_its_own_new_file_and_says_why_it_cannot(tmp_path):
    import os
    shared = tmp_path / "shared"
    shared.mkdir()
    lab = shared / "lab.toml"
    lab.write_text("[a]\nx = 1\n")
    lab.chmod(0o664)
    victim = tmp_path / "notes.txt"
    victim.write_text("private")
    victim.chmod(0o600)
    for name in (f".lab.toml.{os.getpid()}.tmp", ".lab.toml.tmp"):        # a link planted where a temp file goes
        (shared / name).symlink_to(victim)
    tomlfile.update(lab, {("a",): {"x": 2}})
    assert victim.read_text() == "private" and oct(victim.stat().st_mode & 0o777) == "0o600"
    assert not lab.is_symlink() and lab.read_text() == "[a]\nx = 2\n" and oct(lab.stat().st_mode & 0o777) == "0o664"
    new = tmp_path / "new.toml"
    tomlfile.write(new, "x = 1\n")                                           # a new file: as open() makes it
    assert new.stat().st_mode & 0o777 == 0o666 & ~tomlfile._umask()
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "lab.toml").write_text("x = 1\n")
    locked.chmod(0o555)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("root may write anything")
        with pytest.raises(PermissionError, match="folder is read-only"):
            tomlfile.update(locked / "lab.toml", {(): {"x": 2}})
        assert [p.name for p in locked.iterdir()] == ["lab.toml"]
    finally:
        locked.chmod(0o755)


def test_no_stray_blank_lines_where_keys_were():
    assert tomlfile.edit("[a]\r\nx = 1\r\n\r\ny = 2", {("a",): {"y": None}}) == "[a]\r\nx = 1"
    assert tomlfile.edit("[a]\nx = 1\n\ny = 2\n", {("a",): {"y": None}}) == "[a]\nx = 1\n"
    assert tomlfile.edit("x = 1\n\n[a]\ny = 2\n", {(): {"x": None}}) == "[a]\ny = 2\n"
    with pytest.raises(ValueError, match="path of a table"):
        tomlfile.edit("[a]\nx = 1\n", {}, renames={("a",): ()})


def test_a_renamed_tables_old_name_is_a_new_table():
    text = "[d.psu]   # the supply\nport = 1\n\n[d.awg]\nx = 2\n"
    out = tomlfile.edit(text, {("d", "psu"): {"driver": "new:One"}}, renames={("d", "psu"): ("d", "old_psu")})
    assert out == '[d.old_psu]   # the supply\nport = 1\n\n[d.awg]\nx = 2\n\n[d.psu]\ndriver = "new:One"\n'
    assert tomlfile.edit(text, {("d", "psu"): None}, renames={("d", "psu"): ("d", "old_psu")}) == \
        text.replace("[d.psu]", "[d.old_psu]")                              # nothing of that name left to remove


def test_a_table_and_its_sub_table_are_removed_in_either_order():
    text = "[a]\nx = 1\n\n[a.b]\ny = 2\n\n[c]\nz = 3\n"
    assert tomlfile.edit(text, {("a",): None, ("a", "b"): None}) == "[c]\nz = 3\n"
    assert tomlfile.edit(text, {("a", "b"): None, ("a",): None}) == "[c]\nz = 3\n"


def test_removed_then_changed_under_it_and_nested_rename_targets():
    text = "[a]\nx = 1\n\n[a.b]\ny = 2\n"
    assert tomlfile.edit(text, {("a",): None, ("a", "b"): {"y": 5}}) == "[a.b]\ny = 5\n"   # a new [a.b]
    text = "[x]\nv = 0x10\n\n[y]\nw = 1\n"
    for renames in ({("x",): ("d",), ("y",): ("d", "e")}, {("y",): ("d", "e"), ("x",): ("d",)}):
        assert tomlfile.edit(text, {("d", "e"): {"w": 1}}, renames) == "[d]\nv = 0x10\n\n[d.e]\nw = 1\n"


def test_links_of_other_users_are_not_followed_and_a_read_only_file_system_says_so(tmp_path, monkeypatch):
    import errno
    import os
    real = tmp_path / "real.toml"
    real.write_text("x = 1\n")
    link = tmp_path / "lab.toml"
    link.symlink_to(real)
    tomlfile.update(link, {(): {"x": 2}})                                    # this user's link: followed
    assert link.is_symlink() and real.read_text() == "x = 2\n"
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)             # as if another user had made it
    with pytest.raises(PermissionError, match="made by another user"):
        tomlfile.update(link, {(): {"x": 3}})
    assert real.read_text() == "x = 2\n"
    monkeypatch.undo()

    def read_only(*a, **kw):
        raise OSError(errno.EROFS, "Read-only file system")
    monkeypatch.setattr(tomlfile.tempfile, "mkstemp", read_only)
    with pytest.raises(PermissionError, match="read-only file system"):
        tomlfile.write(tmp_path / "new.toml", "x = 1\n")
