"""Other nodes' GPU tasks on page 5 (#14 step 4, D-05): the agent shares
only on --share-tasks, never the handles end/open use, command lines only
on --share-args; the viewer lists them read-only."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import cluster, panels, taskpage, tasks, term  # noqa: E402
from test_cli import run_main  # noqa: E402

G = 1024 ** 3


def _local_tree(pid=4242):
    proc = {"pid": pid, "name": "python3", "cmd": "python3 train.py --api-key SECRET", "user": "alice",
            "cpu": 101.0, "rss": 2 * G, "age": 600.0, "status": "running", "state": "R", "gpu_mem": 20 * G,
            "sm": 95, "membw": 40, "enc": 0, "dec": 0, "gpu_idx": 0, "on_gpus": [0], "holder": False,
            "tty": "pts/3", "pgid": pid, "alive": True}
    grp = {"kind": "tmux", "key": "tmux:%3", "label": "tmux train:0 python3", "detail": "pane 0 · pts/3",
           "target": "train:0.0", "pane_id": "%3", "session": "train", "attachable": True,
           "gpu_mem": 20 * G, "procs": [proc]}
    root = {"idx": 0, "id": "0", "name": "NVIDIA GB10", "util": 96, "mem_used": None, "mem_total": None,
            "enc_util": None, "dec_util": None, "groups": [grp]}
    return {"gpus": [root], "n_procs": 1, "n_orphans": 0, "source": "nvml", "holders": False, "caps": {}}


def _view(name, sample, is_local=False, stale=False, error=None):
    return {"name": name, "sample": sample, "is_local": is_local, "stale": stale, "error": error}


class WireTreeTest(unittest.TestCase):
    def test_no_command_lines_or_handles_by_default(self):
        wire = tasks.wire_tree(_local_tree())
        text = repr(wire)
        self.assertTrue(wire["shared"])
        self.assertFalse(wire["args"])
        self.assertNotIn("SECRET", text)
        self.assertNotIn("cmd", wire["gpus"][0]["groups"][0]["procs"][0])
        for handle in ("target", "pane_id", "train:0.0", "attachable", "pgid"):
            self.assertNotIn(handle, text)
        p = wire["gpus"][0]["groups"][0]["procs"][0]
        self.assertEqual((p["pid"], p["name"], p["user"], p["sm"]), (4242, "python3", "alice", 95))

    def test_share_args_adds_the_command_line(self):
        wire = tasks.wire_tree(_local_tree(), share_args=True)
        self.assertTrue(wire["args"])
        self.assertIn("SECRET", wire["gpus"][0]["groups"][0]["procs"][0]["cmd"])


class PayloadTest(unittest.TestCase):
    def _state(self, share, args=False):
        collector = mock.Mock()
        collector.sample.return_value = _local_tree()
        return SimpleNamespace(share_tasks=share, share_args=args, tasks=collector)

    def test_off_by_default_and_never_samples(self):
        st = self._state(False)
        self.assertEqual(cluster.tasks_payload(st, {"gpus": []}), {"shared": False})
        st.tasks.sample.assert_not_called()
        self.assertEqual(cluster.tasks_payload(SimpleNamespace(), {}), {"shared": False})

    def test_shared(self):
        out = cluster.tasks_payload(self._state(True), {"gpus": [{"id": "0"}]})
        self.assertTrue(out["shared"])
        self.assertNotIn("SECRET", repr(out))
        self.assertIn("SECRET", repr(cluster.tasks_payload(self._state(True, True), {"gpus": []})))

    def test_a_failing_collector_shares_nothing(self):
        st = self._state(True)
        st.tasks.sample.side_effect = RuntimeError("boom")
        self.assertEqual(cluster.tasks_payload(st, {}), {"shared": False})


class MergeTest(unittest.TestCase):
    def _merged(self, args=False):
        remote = {"node": "node-2", "version": "5.6.0",
                  "tasks": tasks.wire_tree(_local_tree(), share_args=args)}
        views = [_view("node-1", {"node": "node-1"}, is_local=True), _view("node-2", remote),
                 _view("node-3", {"node": "node-3", "version": "5.6.0", "tasks": {"shared": False}}),
                 _view("node-4", {"node": "node-4", "version": "5.5.1"}),
                 _view("node-5", None, stale=True, error="http 403")]
        return tasks.merge_remote(_local_tree(), views)

    def test_remote_roots_and_notes(self):
        tree = self._merged()
        roots = tree["gpus"]
        self.assertEqual([r["idx"] for r in roots], [0, "node-2:0", "node-3:-", "node-4:-", "node-5:-"])
        self.assertEqual(tree["n_procs"], 2)
        self.assertEqual(tree["nodes"], 1)
        r2 = roots[1]
        self.assertTrue(r2["remote"])
        self.assertEqual((r2["node"], r2["gpu_no"]), ("node-2", 0))
        p = r2["groups"][0]["procs"][0]
        self.assertTrue(p["remote"] and p["args_hidden"])
        self.assertEqual(p["cmd"], "")
        self.assertEqual(r2["groups"][0]["key"], "node-2:tmux:%3")
        self.assertIn("--share-tasks", roots[2]["note"])
        self.assertIn("5.5.1", roots[3]["note"])
        self.assertIn("http 403", roots[4]["note"])
        self.assertNotIn("remote", roots[0])

    def test_stale_node_keeps_its_last_list(self):
        remote = {"node": "node-2", "tasks": tasks.wire_tree(_local_tree())}
        tree = tasks.merge_remote({"gpus": []}, [_view("node-2", remote, stale=True, error="timed out")])
        self.assertTrue(tree["gpus"][0]["stale"])
        self.assertEqual(tree["n_procs"], 1)

    def test_shared_args_kept(self):
        p = self._merged(args=True)["gpus"][1]["groups"][0]["procs"][0]
        self.assertIn("SECRET", p["cmd"])
        self.assertNotIn("args_hidden", p)

    def test_rows_keep_the_same_pid_apart(self):
        rows = tasks.flatten(self._merged())
        procs = [r for r in rows if r["type"] == "proc"]
        self.assertEqual(len(procs), 2)
        self.assertEqual(procs[0]["proc"]["pid"], procs[1]["proc"]["pid"])
        self.assertFalse(tasks.TaskUI._same_row(procs[0], procs[1]))
        self.assertTrue(procs[1]["remote"])
        self.assertNotIn("remote", procs[0])
        self.assertEqual([r for r in rows if r["type"] == "gpu"][1]["node"], "node-2")

    def test_filter_matches_the_node(self):
        rows = tasks.flatten(self._merged(), filt="node-2")
        self.assertEqual([r["proc"]["node"] for r in rows if r["type"] == "proc"], ["node-2"])


class ReadOnlyTest(unittest.TestCase):
    def setUp(self):
        tree = tasks.merge_remote(_local_tree(), [_view("node-2", {"node": "node-2",
                                                                   "tasks": tasks.wire_tree(_local_tree())})])
        self.rows = tasks.flatten(tree)
        self.ui = tasks.TaskUI()
        self.ui.set_rows(self.rows)
        self.remote_proc = next(i for i, r in enumerate(self.rows) if r["type"] == "proc" and r.get("remote"))
        self.remote_group = next(i for i, r in enumerate(self.rows) if r["type"] == "group" and r.get("remote"))

    def test_end_and_kill_never_arm(self):
        for i in (self.remote_proc, self.remote_group):
            self.ui.selected = i
            for force in (False, True):
                self.ui.arm_end(force=force)
                self.assertFalse(self.ui.confirming)
                self.assertIn("read-only", self.ui.toast[0])

    def test_open_refused(self):
        self.ui.selected = self.remote_group
        self.assertIsNone(self.ui.open_spec())
        self.assertIn("read-only", self.ui.toast[0])

    def test_local_rows_still_arm(self):
        self.ui.selected = next(i for i, r in enumerate(self.rows) if r["type"] == "proc" and not r.get("remote"))
        self.ui.arm_end()
        self.assertTrue(self.ui.confirming)
        self.ui.cancel()

    def test_end_functions_refuse_without_signalling(self):
        proc = self.rows[self.remote_proc]["proc"]
        group = self.rows[self.remote_group]["group"]
        with mock.patch.object(tasks, "_signal_pid") as sig, mock.patch.object(tasks.subprocess, "run") as run:
            ok, msg = tasks.end_process(proc, force=True)
            self.assertFalse(ok)
            self.assertIn("node-2", msg)
            ok, _ = tasks.end_group(group, force=True)
            self.assertFalse(ok)
            argv, _, note = tasks.open_group_argv(group)
            self.assertIsNone(argv)
            sig.assert_not_called()
            run.assert_not_called()


class DrawTest(unittest.TestCase):
    def setUp(self):
        self._style = term.BAR_STYLE
        term.BAR_STYLE = "eighths"

    def tearDown(self):
        term.BAR_STYLE = self._style

    def _text(self, layout=None, w=150, h=40, select_remote=True):
        tree = tasks.merge_remote(_local_tree(), [
            _view("node-2", {"node": "node-2", "tasks": tasks.wire_tree(_local_tree())}),
            _view("node-3", {"node": "node-3", "version": "5.6.0", "tasks": {"shared": False}})])
        rows = tasks.flatten(tree)
        sel = next(i for i, r in enumerate(rows) if r["type"] == "proc" and bool(r.get("remote")) == select_remote)
        ui = dict(tasks.TaskUI().render_ctx(), selected=sel)
        sample = {"gpus": [{"id": "0", "name": "NVIDIA GB10", "util": 96}],
                  "caps": {"gpu": [{"proc_util": True}], "has_nvml": True}, "rate": 1.0}
        track = {"proc": {("0", 4242): {"sm": [90.0] * 30, "mem": [float(20 * G)] * 30}},
                 "cpu": {4242: [100.0] * 30}, "ended": []}
        built = taskpage.build_page5(sample, tree, rows, "wide", w, 0, height=h, ui=ui, track=track,
                                     detail=None, layout=layout)
        vc = term.VirtualCurses(size=(w, h))
        panels.render(vc, built, {i: i for i in range(16)})
        return "\n".join("".join(c for c, _ in row) for row in vc.grid)

    def test_remote_rows_and_notes_show(self):
        text = self._text()
        self.assertIn("node-2 · GPU 0", text)
        self.assertIn("node-3", text)
        self.assertIn("--share-tasks", text)
        self.assertIn("on 2 nodes", text)
        self.assertIn("arguments not shared", text)
        self.assertIn("read-only here", text)
        self.assertIn("view only", text)
        self.assertNotIn("started  ?", text)
        self.assertNotIn("kill (SIGKILL)", text)
        remote_line = next(ln for ln in text.splitlines() if "4242" in ln and "▶" in ln)
        self.assertNotIn("SECRET", remote_line)

    def test_local_selection_keeps_the_action_keys(self):
        self.assertIn("kill (SIGKILL)", self._text(select_remote=False))

    def test_remote_detail_never_borrows_local_history(self):
        row = {"type": "proc", "remote": True, "node": "node-2", "gpu_id": "0", "proc": {"pid": 4242}}
        self.assertIsNone(taskpage._proc_hist({"proc": {("0", 4242): {"mem": [1.0, 2.0]}}}, row))

    def test_big_layout_cards_only_for_local_gpus(self):
        text = self._text(layout="large", w=240, h=67)
        self.assertIn("node-2 · GPU 0", text)
        self.assertEqual(text.count("NVIDIA GB10"), 3)  # local card + local row + node-2's row


class CliTest(unittest.TestCase):
    def test_share_args_needs_share_tasks(self):
        code, _, err = run_main(["--json", "--share-args"])
        self.assertEqual(code, 1)
        self.assertIn("--share-tasks", err)

    def test_share_tasks_needs_serve_or_json(self):
        code, _, err = run_main(["--share-tasks"])
        self.assertEqual(code, 1)
        self.assertIn("--serve or --json", err)

    def test_serve_refuses_to_share_without_a_token(self):
        env = {k: v for k, v in os.environ.items() if k not in ("SPARK_SMI_TOKEN", "SPARK_SMI_TOKEN_FILE")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(cluster, "run_server") as srv:
            code, _, err = run_main(["--serve", "--share-tasks"])
        self.assertEqual(code, 1)
        self.assertIn("SPARK_SMI_TOKEN_FILE", err)
        srv.assert_not_called()

    def test_serve_shares_with_a_token(self):
        with mock.patch.dict(os.environ, {"SPARK_SMI_TOKEN": "t"}), \
                mock.patch.object(cluster, "run_server") as srv:
            code, _, _ = run_main(["--serve", "--share-tasks"])
        self.assertIn(code, (0, None))
        srv.assert_called_once()
        self.assertTrue(srv.call_args[0][0].share_tasks)


if __name__ == "__main__":
    unittest.main()


class ControlCharacterTest(unittest.TestCase):
    """#18: a remote string cannot drive the viewer's terminal."""

    def test_grid_never_holds_control_characters(self):
        vc = term.VirtualCurses(size=(40, 2))
        vc.addstr(0, 0, "ok\x1b]0;pwned\x07\x9b2J end")
        text = "".join(c for c, _ in vc.grid[0])
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("\x9b", text)
        self.assertTrue(text.startswith("ok?]0;pwned??2J end"))
        self.assertNotIn("\x1b]0", vc.render())

    def test_page5_with_a_hostile_label(self):
        wire = tasks.wire_tree(_local_tree())
        wire["gpus"][0]["groups"][0]["label"] = "tmux \x1b[2Jevil"
        tree = tasks.merge_remote({"gpus": []}, [_view("node-2", {"node": "node-2", "tasks": wire})])
        rows = tasks.flatten(tree)
        built = taskpage.build_page5({"gpus": []}, tree, rows, "wide", 150, 0, height=30)
        vc = term.VirtualCurses(size=(150, 30))
        panels.render(vc, built, {i: i for i in range(16)})
        out = vc.render()
        self.assertIn("tmux ?[2Jevil", out)
        self.assertNotIn("\x1b[2J", out)
