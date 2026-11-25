"""insthub's version, in a module of its own: pyproject.toml reads it from here
without importing insthub, the package exports it, and every server announces it
in its hello.  One place to change it."""

__version__ = "0.1.0"
