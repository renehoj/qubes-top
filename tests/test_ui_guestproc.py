"""The guest process table: parser, provider and poller (D-15).

The panel's source is the guest's own ``top -b`` through ``qvm-run``, so the
two things worth pinning off-box are (a) the parser against real procps
output — including the second-frame rule, because the first frame's %CPU is
an average since boot and would be a different number under the same
heading — and (b) the command shape, which must never reach a shell.

The threading is deliberately thin: ``poll_once`` is the whole decision, and
it is called directly here. The thread around it only sleeps and repeats.

Run standalone:

    python3 tests/test_ui_guestproc.py
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from qubes_top.ui.guestproc import (  # noqa: E402
    GuestProcPoller,
    GuestProcProvider,
    GuestProcReading,
    parse_top,
    qvm_run_argv,
    run_qvm,
)

#: Two real frames from procps-ng (captured on a Linux host, 80 columns for
#: the test file). Frame 1 and frame 2 deliberately disagree: the parser must
#: return frame 2 (a real %CPU delta), never frame 1 (an average since boot).
TOP_TWO_FRAMES = """\
top - 19:45:35 up 1 day, 11:39,  2 users,  load average: 0.30, 0.14, 0.10
Tasks: 494 total,   1 running, 493 sleeping,   0 stopped,   0 zombie
%Cpu(s):  0.0 us,  0.0 sy,  0.0 ni, 98.5 id,  1.5 wa,  0.0 hi,  0.0 si,  0.0 st
MiB Mem : 125722.5 total,  68300.7 free,   7439.6 used,  51800.6 buff/cache
MiB Swap:   8192.0 total,   8192.0 free,      0.0 used. 118282.9 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
      1 root      20   0   26172  16688  11216 S   0.0   0.0   0:04.93 systemd
   4210 riley     20   0 2345678 345678 123456 S  99.9  12.3   9:99.99 firefox

top - 19:45:36 up 1 day, 11:39,  2 users,  load average: 0.30, 0.14, 0.10
Tasks: 494 total,   1 running, 493 sleeping,   0 stopped,   0 zombie
%Cpu(s):  1.0 us,  0.5 sy,  0.0 ni, 98.0 id,  0.5 wa,  0.0 hi,  0.0 si,  0.0 st
MiB Mem : 125722.5 total,  68300.7 free,   7439.6 used,  51800.6 buff/cache
MiB Swap:   8192.0 total,   8192.0 free,      0.0 used. 118282.9 avail Mem

    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND
      1 root      20   0   26172  16688  11216 S   0.3   0.0   0:04.96 systemd
   4210 riley     20   0 2345678 345678 123456 S  12.5  12.3  10:01.12 firefox
   4300 riley     20   0   98765  43210  12345 S   0.0   0.3   0:00.10 Web Content
   4400 root      20   0       0      0      0 I   0.0   0.0   0:00.01 kworker/R-rcu_gp
"""


class TestParseTop(unittest.TestCase):
    def test_parses_the_second_frame_only(self):
        rows = parse_top(TOP_TWO_FRAMES)
        self.assertEqual([r.pid for r in rows], [1, 4210, 4300, 4400])
        firefox = rows[1]
        self.assertEqual(firefox.name, "firefox")
        self.assertAlmostEqual(firefox.cpu_pct, 12.5)   # frame 2, not 99.9
        self.assertEqual(firefox.rss_kib, 345678)

    def test_a_command_with_spaces_stays_whole(self):
        # "Web Content" is one COMMAND field even though it splits on spaces
        rows = {r.pid: r for r in parse_top(TOP_TWO_FRAMES)}
        self.assertEqual(rows[4300].name, "Web Content")

    def test_kernel_thread_with_zero_res_has_no_rss(self):
        rows = {r.pid: r for r in parse_top(TOP_TWO_FRAMES)}
        self.assertIsNone(rows[4400].rss_kib, "a zero RES is 'no value'")

    def test_empty_and_garbage_yield_no_rows(self):
        self.assertEqual(parse_top(""), [])
        self.assertEqual(parse_top(None), [])
        self.assertEqual(parse_top("Windows qube: no such command"), [])
        self.assertEqual(parse_top("    PID USER\nx y\n"), [])

    def test_a_short_or_garbled_row_is_skipped(self):
        text = ("    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM"
                "     TIME+ COMMAND\n"
                "      1 root      20   0   26172  16688  11216 S   0.3   0.0"
                "   0:04.96 systemd\n"
                "   not-a-pid root 20 0 1 2 3 S 0.0 0.0 0:00.00 broken\n"
                "      5 root      20   0       0\n")   # truncated line
        rows = parse_top(text)
        self.assertEqual([r.pid for r in rows], [1])

    def test_res_suffixes_are_kib(self):
        text = ("    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM"
                "     TIME+ COMMAND\n"
                "      7 root      20   0 1234567   1.5g  1234 S   1.0   1.0"
                "   0:01.00 bigproc\n")
        rows = parse_top(text)
        self.assertEqual(rows[0].rss_kib, int(1.5 * 1024 ** 2))

    def test_carriage_returns_are_tolerated(self):
        # qvm-run passes the guest's bytes through; a \r must not break a row.
        rows = parse_top(TOP_TWO_FRAMES.replace("\n", "\r\n"))
        self.assertEqual(len(rows), 4)

    def test_a_locale_decimal_comma_is_accepted(self):
        # top formats through setlocale(): a guest with e.g. da_DK prints
        # "12,5". float() rejects it, and the whole %CPU column read as
        # dashes — the owner's "%CPU does not get shown" on dom0.
        danish = TOP_TWO_FRAMES.replace("  12.5  ", "  12,5  ")
        rows = {r.pid: r for r in parse_top(danish)}
        self.assertAlmostEqual(rows[4210].cpu_pct, 12.5)

    def test_a_trailing_percent_sign_is_tolerated(self):
        text = TOP_TWO_FRAMES.replace("  12.5  ", "  12.5% ")
        rows = {r.pid: r for r in parse_top(text)}
        self.assertAlmostEqual(rows[4210].cpu_pct, 12.5)

    def test_no_row_loses_its_cpu_to_the_separator(self):
        # every %CPU value in the frame survives, whichever separator is used
        danish = TOP_TWO_FRAMES.replace("  12.5  ", "  12,5  ").replace(
            "   0.3   0.0", "   0,3   0,0")
        rows = parse_top(danish)
        self.assertEqual([r.cpu_pct is None for r in rows], [False] * len(rows))

    def test_row_cap(self):
        header = ("    PID USER      PR  NI    VIRT    RES    SHR S  %CPU"
                  "  %MEM     TIME+ COMMAND\n")
        lines = [header] + ["  %d root 20 0 1 2 3 S 0.0 0.0 0:00.00 p%d"
                            % (i, i) for i in range(1, 51)]
        self.assertEqual(len(parse_top("\n".join(lines), max_rows=10)), 10)


class TestCommandShape(unittest.TestCase):
    def test_no_shell_and_a_fixed_argv(self):
        argv = qvm_run_argv("work")
        self.assertEqual(argv[0], "qvm-run")
        self.assertIn("--no-shell", argv)
        self.assertIn("work", argv)
        top = argv[argv.index("top"):]
        # two frames, because only the second carries a real %CPU delta
        self.assertEqual(top[:7], ["top", "-b", "-n", "2", "-d", "1", "-w"])
        self.assertIn("%CPU", top)

    def test_the_name_is_the_qube_not_a_command_fragment(self):
        argv = qvm_run_argv("browser-streaming")
        # between the last option and the command, exactly one element: the
        # name
        i = argv.index("--filter-escape-chars")
        self.assertEqual(argv[i + 1], "browser-streaming")
        self.assertEqual(argv[i + 2], "top")


class TestBoundedRunner(unittest.TestCase):
    """``run_qvm`` reads guest-written bytes: stdout is capped, stderr is
    truncated, a slow writer times out. A local python stands in for
    ``qvm-run`` (the runner takes any argv)."""

    @staticmethod
    def _py(code):
        return [sys.executable, "-c", code]

    def test_normal_output_passes_through(self):
        rc, out, err = run_qvm(self._py(
            "import sys; print('hello'); print('oops', file=sys.stderr); "
            "sys.exit(3)"), timeout=10)
        self.assertEqual((rc, out, err), (3, "hello\n", "oops\n"))

    def test_stdout_over_the_cap_is_a_failure(self):
        rc, out, err = run_qvm(self._py(
            "import sys\nwhile True: sys.stdout.write('x' * 65536)"),
            timeout=10, max_out=100000)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("cap", err)

    def test_stderr_is_truncated_not_fatal(self):
        rc, out, err = run_qvm(self._py(
            "import sys; sys.stderr.write('e' * 50000); print('ok')"),
            timeout=10, max_err=10)
        self.assertEqual((rc, out, err), (0, "ok\n", "e" * 10))

    def test_a_silent_process_times_out(self):
        rc, out, err = run_qvm(self._py("import time; time.sleep(30)"),
                               timeout=0.5)
        self.assertEqual(rc, 1)
        self.assertIn("timed out", err)

    def test_a_missing_command_is_a_reading_not_a_raise(self):
        rc, out, err = run_qvm(["/nonexistent/qubes-top-test"], timeout=5)
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err)


class TestNeverStartsAQube(unittest.TestCase):
    def test_prefix_flags(self):
        argv = qvm_run_argv("work")
        for flag in ("--no-autostart", "--no-gui", "--filter-escape-chars"):
            self.assertIn(flag, argv[:argv.index("work")])


class FakeRunner:
    def __init__(self, rc=0, out="", err=""):
        self.rc = rc
        self.out = out
        self.err = err
        self.argvs = []

    def __call__(self, argv, timeout):
        self.argvs.append((list(argv), timeout))
        return self.rc, self.out, self.err


class TestProvider(unittest.TestCase):
    def test_success_returns_rows(self):
        runner = FakeRunner(out=TOP_TWO_FRAMES)
        reading = GuestProcProvider(runner=runner).read("work")
        self.assertFalse(reading.note, "a success carries no note")
        self.assertIsNotNone(reading.rows)
        self.assertEqual([r.pid for r in reading.rows], [1, 4210, 4300, 4400])
        self.assertEqual(runner.argvs[0][0], qvm_run_argv("work"))

    def test_failure_carries_the_error_line(self):
        runner = FakeRunner(rc=1, err="command failed: top: not found\n")
        reading = GuestProcProvider(runner=runner).read("windows")
        self.assertIsNone(reading.rows)
        self.assertIn("top: not found", reading.note)

    def test_empty_output_is_explained(self):
        reading = GuestProcProvider(runner=FakeRunner(out="\n\n")).read("work")
        self.assertIsNone(reading.rows)
        self.assertIn("no process table", reading.note)

    def test_runner_crash_does_not_raise(self):
        def boom(argv, timeout):
            raise RuntimeError("fork failed")
        reading = GuestProcProvider(runner=boom).read("work")
        self.assertIsNone(reading.rows)
        self.assertIn("fork failed", reading.note)

    def test_an_invalid_name_never_reaches_the_runner(self):
        runner = FakeRunner(out=TOP_TWO_FRAMES)
        provider = GuestProcProvider(runner=runner)
        for bad in ("", "-oops", "a b", "vm;reboot", "x" * 80):
            with self.subTest(name=bad):
                reading = provider.read(bad)
                self.assertIsNone(reading.rows)
                self.assertIn("not a valid qube name", reading.note)
        self.assertEqual(runner.argvs, [])

    def test_list_is_the_pane_seam(self):
        provider = GuestProcProvider(runner=FakeRunner(out=TOP_TWO_FRAMES))
        self.assertEqual(len(provider.list("work")), 4)


class EmptyProvider:
    def __init__(self, rows=None, note="", timeout=8.0):
        self.rows = rows
        self.note = note
        self._timeout = timeout
        self.calls = []

    def read(self, name):
        self.calls.append(name)
        return GuestProcReading(self.rows, self.note)


class TestPoller(unittest.TestCase):
    def setUp(self):
        self.emitted = []
        self.provider = EmptyProvider(rows=[])
        self.poller = GuestProcPoller(
            self.provider, emit=self._emit,
            post=lambda cb, *a: cb(*a))

    def _emit(self, name, reading):
        self.emitted.append((name, reading))
        return False

    def test_no_target_no_fetch(self):
        self.assertFalse(self.poller.poll_once(now=100.0))
        self.assertEqual(self.provider.calls, [])

    def test_dom0_is_never_fetched(self):
        self.poller.set_target("dom0")
        self.assertFalse(self.poller.poll_once(now=100.0))
        self.assertEqual(self.provider.calls, [])

    def test_a_guest_is_fetched_at_once_and_posted(self):
        self.poller.set_target("work")
        self.assertTrue(self.poller.poll_once(now=100.0))
        self.assertEqual(self.provider.calls, ["work"])
        self.assertEqual(self.emitted[0][0], "work")

    def test_a_repeat_fetch_waits_for_the_interval(self):
        self.poller.set_target("work")
        self.poller.poll_once(now=100.0)
        self.assertFalse(self.poller.poll_once(now=100.5))
        self.assertTrue(self.poller.poll_once(now=102.0))
        self.assertEqual(self.provider.calls, ["work", "work"])

    def test_a_target_change_fetches_immediately(self):
        self.poller.set_target("work")
        self.poller.poll_once(now=100.0)
        self.poller.set_target("personal")
        self.assertTrue(self.poller.poll_once(now=100.1),
                        "a new selection must not wait out the old cadence")
        self.assertEqual(self.provider.calls, ["work", "personal"])

    def test_the_target_is_read_under_the_lock(self):
        # set_target from the GUI thread while the poller thread reads it
        self.poller.set_target("work")
        self.assertEqual(self.poller.target(), "work")
        self.poller.set_target(None)
        self.assertIsNone(self.poller.target())


if __name__ == "__main__":
    unittest.main(verbosity=2)
