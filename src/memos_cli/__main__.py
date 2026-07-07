"""Allow running with ``python -m memos_cli``.

This is one of three entry paths that must UTF-8-configure Python's std streams
before Rich / Typer / Click construction — the others are the pip
console-script (handled by ``main.py`` at import time) and the PyInstaller
frozen binary (handled by the ``_pyi_rth_utf8`` runtime hook).

The call below runs *before* ``from memos_cli.main import app`` so no
downstream module can capture a stale (GBK) ``sys.stdout`` reference. The
duplicate call inside ``main.py`` is *not* dead code: it's the pip
console-script path's own bootstrap, and the ``_APPLIED`` guard inside
``ensure_utf8_stdio`` makes the redundant hit here a no-op (fix #3).
"""
from memos_cli.encoding_bootstrap import ensure_utf8_stdio

ensure_utf8_stdio()

from memos_cli.main import app  # noqa: E402 — must run after ensure_utf8_stdio()

if __name__ == "__main__":
    app()
