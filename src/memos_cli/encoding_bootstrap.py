"""UTF-8 stdio bootstrap for Windows-frozen CLI.

Windows chooses the process's active code page (typically CP936 / GBK on
Chinese systems) for ``sys.stdout``/``stderr``/``stdin`` unless the app opts in
to UTF-8. That default corrupts Rich-rendered CJK output from the CLI, so we
force UTF-8 as early as possible on every entry path (PyInstaller runtime hook,
``python -m memos_cli``, and the pip console-script entry).

Design notes
------------
* The reconfiguration is *idempotent* — ``_APPLIED`` short-circuits repeat
  calls, so wiring it from multiple entry points is cheap. Critically, we only
  latch ``_APPLIED`` on the success path (see fix #9/#11 in the OCR review):
  if the very first invocation (e.g. from the PyInstaller hook) fails mid-way,
  the later, more reliable in-package calls still get a chance to complete
  the work.
* The wrapper swap detaches the old ``TextIOWrapper`` from its underlying
  binary buffer before handing that buffer to the new UTF-8 wrapper. Without
  the detach, garbage collection of the old wrapper can flush/close the same
  buffer the new wrapper is writing into (fix #8).
* All ctypes / kernel32 fallbacks catch ``Exception`` rather than a narrow
  set — CPython on Windows can surface ``ctypes.ArgumentError`` and other
  non-``OSError`` errors that would otherwise escape (fix #10). The module's
  contract is "never brick the launcher", so broad swallowing is the correct
  policy here.
"""
from __future__ import annotations

import io
import os
import sys

_APPLIED = False


def _is_utf8_encoding(encoding: str | None) -> bool:
    """Normalise and match against ``utf8`` (also treats ``utf-8-sig`` as UTF-8)."""
    if not encoding:
        return False
    return encoding.lower().replace("-", "").replace("_", "").startswith("utf8")


def _reconfigure_stream(stream_name: str, *, errors: str) -> None:
    """Rewrap ``sys.<stream_name>`` in a UTF-8 ``TextIOWrapper``.

    Idempotent per-stream: if the current stream already declares a UTF-8
    encoding we only ``reconfigure(errors=...)`` on it (so callers can pin
    ``strict`` on stdin or ``replace`` on stdout/stderr) instead of swapping
    the object out — swapping would invalidate any reference already captured
    by the harness or a test.
    """
    stream = getattr(sys, stream_name, None)
    if stream is None:
        return

    current_encoding = getattr(stream, "encoding", None)
    if _is_utf8_encoding(current_encoding):
        # Already UTF-8 — just pin the error policy without swapping the object.
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors=errors)
            except Exception:  # noqa: BLE001 — never fail the CLI
                pass
        return

    # Sever the old wrapper's ownership of the underlying binary buffer before
    # we hand the buffer to a new wrapper. Without ``detach()``, the old
    # wrapper's ``__del__`` may flush/close the same buffer at GC time —
    # particularly ugly during interpreter teardown (fix #8).
    buffer = None
    detach = getattr(stream, "detach", None)
    if callable(detach):
        try:
            buffer = detach()
        except Exception:  # noqa: BLE001 — stream may not support detach
            buffer = None
    if buffer is None:
        buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return

    try:
        wrapped = io.TextIOWrapper(
            buffer,
            encoding="utf-8",
            errors=errors,
            line_buffering=getattr(stream, "line_buffering", False),
            write_through=getattr(stream, "write_through", False),
        )
    except Exception:  # noqa: BLE001 — never fail the CLI
        return

    setattr(sys, stream_name, wrapped)


def _set_windows_console_utf8() -> None:
    """Align the Windows console code page with our UTF-8 Python streams.

    Without this, C-level writes (e.g. from a native extension) still go
    through the old ACP and produce mojibake even after Python's own streams
    have been switched to UTF-8. The ctypes calls are wrapped broadly because
    CPython may raise ``ctypes.ArgumentError`` — a non-``OSError`` — in some
    environments (fix #10).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        try:
            kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001 — never fail the CLI
            pass
        try:
            kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001 — never fail the CLI
            pass
    except Exception:  # noqa: BLE001 — ctypes unavailable / kernel32 missing / wrong platform quirks
        pass


def _emit_diagnostic_if_needed() -> None:
    """One-shot debug hint when we detect a Windows non-UTF-8 setup.

    Rich's ``Console`` captures ``sys.stdout``/``stderr`` at construction time,
    so if any step of the reconfiguration silently no-ops the operator ends up
    with mojibake and no clue why. We write once to the *raw* ``sys.__stderr__``
    (which is not wrapped by anything) so at least the diagnostic itself is
    readable in whatever the terminal supports. Only fires on Windows and only
    when either stdout or stderr is still non-UTF-8 after our best effort
    (fix #5).
    """
    if sys.platform != "win32":
        return
    if os.environ.get("MEMOS_CLI_QUIET_ENCODING_DIAG"):
        return
    raw_err = getattr(sys, "__stderr__", None)
    if raw_err is None:
        return
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        if not _is_utf8_encoding(getattr(stream, "encoding", None)):
            try:
                raw_err.write(
                    f"[memos] WARNING: sys.{name} is {stream.encoding!r} "
                    "after UTF-8 bootstrap; CJK output may be corrupted. "
                    "Set MEMOS_CLI_QUIET_ENCODING_DIAG=1 to suppress.\n"
                )
            except Exception:  # noqa: BLE001
                pass
            return


def ensure_utf8_stdio() -> None:
    """Force UTF-8 on Python's std streams and the Windows console.

    Safe to call from any entry point and safe to call more than once. On the
    first successful invocation ``_APPLIED`` latches to ``True`` so subsequent
    calls are a fast no-op. On a partial failure ``_APPLIED`` stays ``False``,
    giving the later, more reliable entry-point calls a chance to complete the
    reconfiguration (fix #9/#11).
    """
    global _APPLIED
    if _APPLIED:
        return

    success = False
    try:
        # 1. Env defaults — propagate UTF-8 to any subprocess we later spawn.
        # ``setdefault`` preserves an explicit user override such as
        # ``PYTHONUTF8=0``.
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        os.environ.setdefault("PYTHONUTF8", "1")

        # 2. In-process Python streams — pivot from CP936 (or whatever the
        # ACP dictated) to UTF-8. Strict on stdin so bad input is visible;
        # replace on write streams so an encoding hiccup can't crash us
        # while we're already trying to report an error.
        _reconfigure_stream("stdin", errors="strict")
        _reconfigure_stream("stdout", errors="replace")
        _reconfigure_stream("stderr", errors="replace")

        # 3. Windows console CP — keep C-level writes aligned with Python.
        _set_windows_console_utf8()

        success = True
    except Exception:  # noqa: BLE001 — never fail the CLI
        pass
    finally:
        # Only latch on success so a transient failure in one entry point
        # (e.g. the PyInstaller runtime hook) doesn't permanently silence
        # the later ``__main__.py`` / ``main.py`` calls (fix #9/#11).
        if success:
            _APPLIED = True

    _emit_diagnostic_if_needed()
