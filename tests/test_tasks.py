"""spark_smi.tasks (page 5): tree flattening, session resolution, the
open/end argv choice, and TaskUI's confirm gate. Nothing here signals a
process: end_process/end_group are always replaced by recording mocks."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import tasks  # noqa: E402


def stat(pid, comm, ppid, tty_nr=0):
    return {"pid": pid, "comm": comm, "state": "S", "ppid": ppid, "pgid": pid, "sid": pid, "tty_nr": tty_nr}


class _Fixed:
    def __init__(self, val):
        self.val = val

    def get(self):
        return self.val


def collector(panes=None, screens=None):
    c = tasks.TaskCollector.__new__(tasks.TaskCollector)
    c._tmux, c._screen, c._docker = _Fixed(panes or {}), _Fixed(screens or {}), _Fixed({})
    return c


def resolve(c, pid, procs):
    """_resolve_session over a fake /proc table, with no cgroup unit."""
    with mock.patch.object(tasks, "_PROCFS", True), \
            mock.patch.object(tasks, "_read_stat", side_effect=lambda p: procs.get(p)), \
            mock.patch.object(tasks, "_read_cgroup_unit", return_value=(None, None, False)):
        return c._resolve_session(pid, {}, {})[0]


PANE = {"session": "work", "window": "0", "window_name": "python3", "pane": "0",
        "pane_id": "%3", "tty": "pts/6"}


class ResolveSessionTest(unittest.TestCase):
    def test_process_that_is_the_pane_command(self):
        # `tmux new -d "python3 train.py"`: no shell between, so the GPU
        # process IS the pane process. It belongs to that pane, not to
        # "another user's tmux server".
        procs = {200: stat(200, "python3", 100), 100: stat(100, "tmux: server", 1), 1: stat(1, "systemd", 0)}
        g = resolve(collector(panes={200: PANE}), 200, procs)
        self.assertEqual(g["kind"], "tmux")
        self.assertTrue(g["attachable"])
        self.assertEqual(g["target"], "work:0.0")

    def test_process_under_a_shell_in_the_pane(self):
        procs = {300: stat(300, "python3", 200), 200: stat(200, "bash", 100),
                 100: stat(100, "tmux: server", 1), 1: stat(1, "systemd", 0)}
        g = resolve(collector(panes={200: PANE}), 300, procs)
        self.assertEqual((g["kind"], g["attachable"]), ("tmux", True))

    def test_other_users_tmux_server_is_not_attachable(self):
        procs = {300: stat(300, "llama-server", 200), 200: stat(200, "bash", 100),
                 100: stat(100, "tmux: server", 1), 1: stat(1, "systemd", 0)}
        with mock.patch.object(tasks, "_uid_name", return_value="someone"), \
                mock.patch.object(tasks.os, "stat", return_value=mock.Mock(st_uid=4242)):
            g = resolve(collector(), 300, procs)
        self.assertEqual(g["kind"], "tmux")
        self.assertFalse(g["attachable"])
        self.assertIn("someone", g["label"])

    def test_parent_gone_is_orphan(self):
        procs = {300: stat(300, "python3", 1), 1: stat(1, "systemd", 0)}
        g = resolve(collector(), 300, procs)
        self.assertEqual(g["kind"], "orphan")


class FlattenTest(unittest.TestCase):
    def test_order_and_last_flags(self):
        tree = {"gpus": [{"idx": 0, "groups": [
            {"kind": "tmux", "procs": [{"pid": 1}, {"pid": 2}]},
            {"kind": "tty", "procs": [{"pid": 3}]}]}]}
        rows = tasks.flatten(tree)
        self.assertEqual([r["type"] for r in rows], ["gpu", "group", "proc", "proc", "group", "proc"])
        self.assertEqual([r.get("last") for r in rows], [None, False, False, True, True, True])
        self.assertTrue(rows[5]["group_last"])

    def test_empty(self):
        self.assertEqual(tasks.flatten(None), [])
        self.assertEqual(tasks.flatten({"gpus": []}), [])


class OpenArgvTest(unittest.TestCase):
    def test_tmux(self):
        g = {"kind": "tmux", "attachable": True, "target": "work:0.0"}
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tasks.open_group_argv(g), (["tmux", "attach-session", "-t", "work:0.0"], True, None))
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1/default,1,0"}):
            argv, blocking, _ = tasks.open_group_argv(g)
        self.assertEqual((argv[:2], blocking), (["tmux", "switch-client"], False))

    def test_refusals_say_why(self):
        argv, _, note = tasks.open_group_argv({"kind": "tmux", "attachable": False})
        self.assertIsNone(argv)
        self.assertIn("another user", note)
        argv, _, note = tasks.open_group_argv({"kind": "orphan", "label": "orphaned"})
        self.assertIsNone(argv)
        self.assertIn("orphaned", note)


def ui_on(rows, selected):
    ui = tasks.TaskUI()
    ui.last_rows = rows
    ui.selected = selected
    return ui


class TaskUIConfirmTest(unittest.TestCase):
    ROWS = [{"type": "gpu", "gpu_idx": 0, "gpu": {"idx": 0}},
            {"type": "group", "gpu_idx": 0, "group": {"kind": "tmux", "label": "tmux work:0", "attachable": True,
                                                      "procs": [{"pid": 7}]}},
            {"type": "proc", "gpu_idx": 0, "group": {}, "proc": {"pid": 7, "name": "python3"}}]

    def setUp(self):
        self.calls = []
        self.fp = lambda p, force: (self.calls.append(("proc", p["pid"], force)) or (True, "sent"))
        self.fg = lambda g, force: (self.calls.append(("group", g["label"], force)) or (True, "ended"))

    def test_nothing_happens_without_y(self):
        ui = ui_on(self.ROWS, 2)
        ui.arm_end()
        self.assertTrue(ui.confirming)
        self.assertIn("SIGTERM", ui.confirm_text)
        ui.move(-1)                      # moving is ignored while armed
        self.assertEqual(ui.selected, 2)
        ui.cancel()                      # any other key cancels
        self.assertIsNone(ui.confirm_yes(self.fp, self.fg))
        self.assertEqual(self.calls, [])

    def test_y_ends_the_armed_process_once(self):
        ui = ui_on(self.ROWS, 2)
        ui.arm_end(force=True)
        self.assertIn("SIGKILL", ui.confirm_text)
        self.assertEqual(ui.confirm_yes(self.fp, self.fg), (True, "sent"))
        self.assertEqual(self.calls, [("proc", 7, True)])
        self.assertIsNone(ui.confirm_yes(self.fp, self.fg))   # disarmed after use
        self.assertEqual(len(self.calls), 1)

    def test_group_row_ends_through_its_manager(self):
        ui = ui_on(self.ROWS, 1)
        ui.arm_end()
        self.assertIn("kills the tmux pane", ui.confirm_text)
        ui.confirm_yes(self.fp, self.fg)
        self.assertEqual(self.calls, [("group", "tmux work:0", False)])

    def test_gpu_row_cannot_be_ended(self):
        ui = ui_on(self.ROWS, 0)
        ui.arm_end()
        self.assertFalse(ui.confirming)
        self.assertIsNotNone(ui.toast)

    def test_failure_is_a_toast_not_a_crash(self):
        def boom(p, force):
            raise OSError("nope")
        ui = ui_on(self.ROWS, 2)
        ui.arm_end()
        ok, msg = ui.confirm_yes(boom, self.fg)
        self.assertFalse(ok)
        self.assertEqual(ui.toast[1], 4)


if __name__ == "__main__":
    unittest.main()
