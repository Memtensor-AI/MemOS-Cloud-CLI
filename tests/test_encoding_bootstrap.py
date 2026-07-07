"""Unit tests for ``memos_cli.encoding_bootstrap``.

The module reconfigures ``sys.stdin`` / ``stdout`` / ``stderr`` in place, so
these tests save the originals in ``setUp`` and restore them in ``tearDown``.
Each test also imports the module *fresh* via ``importlib.reload`` (see
``_fresh_bootstrap``) so the ``_APPLIED`` guard doesn't carry state between
tests.

Fixes applied vs. the OCR review:

* #12 — ``io.BytesIO`` is passed directly to ``TextIOWrapper``; wrapping it
  in ``io.BufferedReader`` raises ``AttributeError`` (``BytesIO`` has no
  ``.readinto`` on the raw layer that ``BufferedReader`` expects).
* #13 — the idempotency test is split: one test proves *in-place* idempotency
  (a second call on the same stream leaves it identical), a separate test
  exercises the ``_APPLIED`` short-circuit path.
* #14 — the "already-UTF-8" test now asserts the ``errors`` policy is set to
  ``replace``, catching the intentional-but-silent mutation from ``strict``.
* #15 — the encoding-normalisation check is extracted into
  ``_assert_utf8`` so future tightening (also checking ``errors``) is a
  one-line change.
"""
from __future__ import annotations

import importlib
import io
import sys
import types
import unittest


def _make_gbk_stream(*, readable: bool = False) -> io.TextIOWrapper:
    """Build a GBK-encoded text stream backed by an in-memory buffer.

    ``io.BytesIO`` is itself a buffered stream, so it can be handed straight
    to ``TextIOWrapper`` — the ``io.BufferedReader(BytesIO(...))`` idiom
    raises ``AttributeError`` because ``BytesIO`` lacks the raw-layer
    ``readinto`` that ``BufferedReader`` expects (fix #12).
    """
    if readable:
        buffer: io.IOBase = io.BytesIO("测试".encode("gbk"))
    else:
        buffer = io.BufferedWriter(io.BytesIO())
    return io.TextIOWrapper(buffer, encoding="gbk", errors="strict")


def _fresh_bootstrap() -> types.ModuleType:
    """Reload the module so ``_APPLIED`` starts as ``False`` in each test."""
    from memos_cli import encoding_bootstrap

    return importlib.reload(encoding_bootstrap)


class EncodingBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            "stdin": sys.stdin,
            "stdout": sys.stdout,
            "stderr": sys.stderr,
        }

    def tearDown(self) -> None:
        sys.stdin = self._saved["stdin"]
        sys.stdout = self._saved["stdout"]
        sys.stderr = self._saved["stderr"]

    # -- helpers ---------------------------------------------------------

    def _assert_utf8(self, stream, *, errors: str | None = None) -> None:
        """Assert ``stream`` is UTF-8 and (optionally) has the given ``errors``.

        Extracted per OCR fix #15 so the six+ call sites don't each repeat
        the ``encoding.lower().replace("-", "") == "utf8"`` idiom.
        """
        self.assertEqual(stream.encoding.lower().replace("-", ""), "utf8")
        if errors is not None:
            self.assertEqual(stream.errors, errors)

    # -- reconfiguration paths ------------------------------------------

    def test_reconfigures_gbk_stdout_to_utf8(self) -> None:
        sys.stdout = _make_gbk_stream()
        bootstrap = _fresh_bootstrap()

        bootstrap.ensure_utf8_stdio()

        self._assert_utf8(sys.stdout, errors="replace")

        # Round-trip: CJK text encodes to UTF-8 bytes on the underlying buffer.
        sys.stdout.write("测试")
        sys.stdout.flush()
        # Walk down to the BytesIO — TextIOWrapper -> BufferedWriter -> BytesIO.
        raw = sys.stdout.buffer.raw if hasattr(sys.stdout.buffer, "raw") else sys.stdout.buffer
        self.assertEqual(raw.getvalue(), "测试".encode("utf-8"))

    def test_reconfigures_gbk_stderr_to_utf8(self) -> None:
        sys.stderr = _make_gbk_stream()
        bootstrap = _fresh_bootstrap()

        bootstrap.ensure_utf8_stdio()

        self._assert_utf8(sys.stderr, errors="replace")

    def test_reconfigures_gbk_stdin_to_utf8(self) -> None:
        sys.stdin = _make_gbk_stream(readable=True)
        bootstrap = _fresh_bootstrap()

        bootstrap.ensure_utf8_stdio()

        self._assert_utf8(sys.stdin, errors="strict")

    # -- already-UTF-8 stream (OCR fix #14) ------------------------------

    def test_leaves_utf8_streams_untouched_object_but_pins_errors_policy(self) -> None:
        """UTF-8 streams keep their identity; ``errors`` is intentionally pinned.

        A test/harness that captured a reference to ``sys.stdout`` before the
        bootstrap ran would break if we swapped the object out, so the module
        preserves identity for already-UTF-8 streams. However, it *does* call
        ``reconfigure(errors="replace")`` (or ``"strict"`` on stdin), and the
        old test hid that mutation. We assert it explicitly here so a future
        change to the errors policy shows up as a test failure (fix #14).
        """
        utf8_stream = io.TextIOWrapper(
            io.BufferedWriter(io.BytesIO()), encoding="utf-8", errors="strict"
        )
        sys.stdout = utf8_stream
        bootstrap = _fresh_bootstrap()

        bootstrap.ensure_utf8_stdio()

        self.assertIs(sys.stdout, utf8_stream)
        self._assert_utf8(sys.stdout, errors="replace")

    def test_leaves_utf8_stdin_untouched_and_pins_strict(self) -> None:
        utf8_stdin = io.TextIOWrapper(
            io.BytesIO(b"ok"), encoding="utf-8", errors="replace"
        )
        sys.stdin = utf8_stdin
        bootstrap = _fresh_bootstrap()

        bootstrap.ensure_utf8_stdio()

        self.assertIs(sys.stdin, utf8_stdin)
        self._assert_utf8(sys.stdin, errors="strict")

    # -- idempotency (OCR fix #13) --------------------------------------

    def test_second_call_on_same_stream_is_a_noop(self) -> None:
        """True in-place idempotency: same object, same encoding, second time."""
        sys.stdout = _make_gbk_stream()
        bootstrap = _fresh_bootstrap()

        bootstrap.ensure_utf8_stdio()
        first = sys.stdout

        bootstrap.ensure_utf8_stdio()

        # The object we captured after the first call is still ``sys.stdout``,
        # and it's still UTF-8 with the same errors policy.
        self.assertIs(sys.stdout, first)
        self._assert_utf8(sys.stdout, errors="replace")

    def test_applied_guard_short_circuits_subsequent_calls(self) -> None:
        """After success, ``_APPLIED`` latches — a new GBK stream is *not* touched.

        This is the ``_APPLIED`` short-circuit behaviour, tested independently
        of the in-place idempotency case above (fix #13).
        """
        sys.stdout = _make_gbk_stream()
        bootstrap = _fresh_bootstrap()

        bootstrap.ensure_utf8_stdio()
        self.assertTrue(bootstrap._APPLIED)

        sys.stdout = _make_gbk_stream()  # a fresh GBK stream
        bootstrap.ensure_utf8_stdio()

        # Because ``_APPLIED`` short-circuited the second call, the freshly
        # swapped-in GBK stream was left alone.
        self.assertEqual(sys.stdout.encoding, "gbk")

    # -- env defaults ---------------------------------------------------

    def test_sets_python_env_defaults(self) -> None:
        import os

        # Clear any pre-existing values so we can observe the setdefault.
        saved_utf8 = os.environ.pop("PYTHONUTF8", None)
        saved_ioenc = os.environ.pop("PYTHONIOENCODING", None)
        try:
            bootstrap = _fresh_bootstrap()
            bootstrap.ensure_utf8_stdio()
            self.assertEqual(os.environ.get("PYTHONUTF8"), "1")
            self.assertEqual(os.environ.get("PYTHONIOENCODING"), "utf-8")
        finally:
            if saved_utf8 is None:
                os.environ.pop("PYTHONUTF8", None)
            else:
                os.environ["PYTHONUTF8"] = saved_utf8
            if saved_ioenc is None:
                os.environ.pop("PYTHONIOENCODING", None)
            else:
                os.environ["PYTHONIOENCODING"] = saved_ioenc

    def test_preserves_explicit_python_env_overrides(self) -> None:
        """``setdefault`` semantics — an explicit override survives the bootstrap."""
        import os

        saved_utf8 = os.environ.get("PYTHONUTF8")
        try:
            os.environ["PYTHONUTF8"] = "0"
            bootstrap = _fresh_bootstrap()
            bootstrap.ensure_utf8_stdio()
            self.assertEqual(os.environ.get("PYTHONUTF8"), "0")
        finally:
            if saved_utf8 is None:
                os.environ.pop("PYTHONUTF8", None)
            else:
                os.environ["PYTHONUTF8"] = saved_utf8


if __name__ == "__main__":
    unittest.main()
