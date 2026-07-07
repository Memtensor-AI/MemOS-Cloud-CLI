"""PyInstaller runtime hook — UTF-8 stdio bootstrap.

Runs before the frozen application's main script, i.e. before Rich's
``Console`` (or any other library) captures ``sys.stdout``/``stderr``. That
early positioning is exactly what makes the fix work for PyInstaller-frozen
``memos.exe``: by the time user code runs, the streams are already UTF-8.

Never fail the launcher. A broken hook that raised an unhandled exception
would prevent the CLI from starting at all. But we distinguish ``ImportError``
from arbitrary ``Exception`` — a missing ``memos_cli.encoding_bootstrap`` in
the frozen binary is a build-config bug we *want* to see, not silently
swallow, because it produces mojibake with no other symptom. All other
exceptions are still suppressed to preserve the "never brick the launcher"
guarantee (fix #2). ``sys.__stderr__`` is the pre-hook raw stream, so the
diagnostic is readable regardless of any wrapper state.
"""
try:
    from memos_cli.encoding_bootstrap import ensure_utf8_stdio

    ensure_utf8_stdio()
except ImportError as exc:  # noqa: BLE001 — build-time misconfig, must be visible
    import sys as _sys

    _raw_err = getattr(_sys, "__stderr__", None)
    if _raw_err is not None:
        try:
            _raw_err.write(
                "[memos] WARNING: UTF-8 stdio bootstrap not bundled "
                f"(import failed: {exc}). CJK output may be corrupted on Windows. "
                "This is a packaging bug — please report it.\n"
            )
        except Exception:  # noqa: BLE001
            pass
except Exception:  # noqa: BLE001 — a broken hook must never break the launcher
    # The in-package bootstrap in ``__main__.py`` / ``main.py`` still runs and
    # gives the CLI its UTF-8 stdio, just slightly later — no CJK regression
    # for users, but we lose the very-earliest reconfiguration.
    pass
