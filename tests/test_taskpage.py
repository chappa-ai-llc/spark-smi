"""Page 5's fuller task manager (#14 step 2): per-process utilization from
NVML, the ProcTracker history and recently-ended list, proc_detail's
environment allow-list, sort/flat/filter, and the page's layouts. Nothing
here signals a process or touches a real GPU."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import collectors, fabtest, panels, pages, taskpage, tasks, term  # noqa: E402

GiB = 1024 ** 3


def proc(pid, name, sm=None, mem=1.0, cpu=0.0, age=100, user="ml", cmd=None):
    p = {"pid": pid, "name": name, "user": user, "gpu_mem": int(mem * GiB), "cpu": cpu, "rss": GiB,
         "age": age, "cmd": cmd or f"{name} --run", "alive": True}
    if sm is not None:
        p.update(sm=sm, membw=0, enc=0, dec=0)
    return p


def group(key, label, procs, kind="tmux"):
    return {"kind": kind, "key": key, "label": label, "detail": "", "procs": procs,
            "gpu_mem": sum(p["gpu_mem"] for p in procs)}


def tree(sm=True):
    s = (lambda v: v) if sm else (lambda v: None)
    return {"gpus": [
        {"idx": 0, "id": "0", "name": "GPU A", "util": 90, "mem_total": 24 * GiB, "groups": [
            group("t:1", "tmux work:0", [proc(10, "python", s(80), 18, 100, 300), proc(11, "python", s(5), 2, 5, 50)]),
            group("s:1", "ssh session pts/1", [proc(20, "ffmpeg", s(30), 1, 20, 900)], kind="ssh"),
        ]},
        {"idx": 1, "id": "1", "name": "GPU B", "util": 10, "mem_total": None, "groups": [
            group("d:1", "docker serve", [proc(30, "vllm", s(10), 40, 30, 5000, user="root")], kind="docker"),
        ]}], "n_procs": 4, "n_orphans": 0, "source": "NVML", "caps": {}}


def sample(sm=True, enc=True):
    return {"gpus": [{"id": "0", "util": 90, "mem_used": 20 * GiB, "mem_total": 24 * GiB,
                      "enc_util": 0 if enc else None, "dec_util": 0 if enc else None},
                     {"id": "1", "util": 10}],
            "caps": {"gpu": [{"proc_util": sm, "mem_local": True}, {"proc_util": sm, "mem_local": False}],
                     "has_nvml": True},
            "driver": "1", "cuda": "1", "rate": 1.0}


def render(built, w, h):
    v = term.VirtualCurses(size=(w, h))
    panels.render(v, built, {i: i for i in range(20)})
    return ["".join(c for c, _ in r).rstrip() for r in v.grid]


class _Err(Exception):
    pass


def nvml_error(name):
    return type(name, (_Err,), {})()


class _Sample:
    def __init__(self, pid, sm, ts):
        self.pid, self.smUtil, self.memUtil, self.encUtil, self.decUtil, self.timeStamp = pid, sm, 0, 0, 0, ts


class ProcUtilTest(unittest.TestCase):
    def setUp(self):
        collectors._PUTIL_LAST.clear()

    def call(self, effect):
        fake = mock.Mock()
        fake.nvmlDeviceGetProcessUtilization = mock.Mock(side_effect=effect)
        with mock.patch.object(collectors, "pynvml", fake, create=True):
            return collectors._proc_util("h", 0)

    def test_newest_sample_per_pid(self):
        out = self.call(lambda h, since: [_Sample(5, 10, 1), _Sample(5, 70, 3), _Sample(6, 4, 2)])
        self.assertEqual(out[5]["sm"], 70)
        self.assertEqual(out[6], {"sm": 4, "membw": 0, "enc": 0, "dec": 0})

    def test_not_found_is_idle_not_unsupported(self):
        def raise_nf(h, since):
            raise nvml_error("NVMLError_NotFound")
        self.assertEqual(self.call(raise_nf), {})

    def test_refused_is_unsupported(self):
        def raise_ns(h, since):
            raise nvml_error("NVMLError_NotSupported")
        self.assertIsNone(self.call(raise_ns))

    def test_never_looks_back_more_than_three_seconds(self):
        seen = []
        collectors._PUTIL_LAST[0] = 1          # a mark from long ago
        self.call(lambda h, since: seen.append(since) or [])
        self.assertGreater(seen[0], (time.time() - 4) * 1e6)


class FlattenTest(unittest.TestCase):
    def test_default_keeps_session_groups(self):
        rows = tasks.flatten(tree())
        self.assertEqual([r["type"] for r in rows][:5], ["gpu", "group", "proc", "proc", "group"])

    def test_sort_by_sm_orders_processes_and_groups(self):
        rows = tasks.flatten(tree(), sort="sm")
        pids = [r["proc"]["pid"] for r in rows if r["type"] == "proc" and r["gpu_idx"] == 0]
        self.assertEqual(pids, [10, 11, 20])
        rows = tasks.flatten(tree(), sort="pid")
        self.assertEqual([r["proc"]["pid"] for r in rows if r["type"] == "proc"], [10, 11, 20, 30])

    def test_flat_drops_group_rows_but_keeps_the_group(self):
        rows = tasks.flatten(tree(), sort="sm", flat=True)
        self.assertNotIn("group", [r["type"] for r in rows])
        procs = [r for r in rows if r["type"] == "proc"]
        self.assertEqual([r["proc"]["pid"] for r in procs], [10, 20, 11, 30])
        self.assertEqual(procs[1]["group"]["label"], "ssh session pts/1")

    def test_filter_matches_pid_name_command_user_and_session(self):
        for filt, want in (("ffmpeg", [20]), ("30", [30]), ("ROOT", [30]), ("work:0", [10, 11]), ("--run", [10, 11, 20, 30])):
            rows = tasks.flatten(tree(), filt=filt)
            self.assertEqual(sorted(r["proc"]["pid"] for r in rows if r["type"] == "proc"), want, filt)


class TaskUIViewTest(unittest.TestCase):
    def test_sort_keeps_the_selected_process(self):
        ui = tasks.TaskUI()
        rows = tasks.flatten(tree())
        ui.set_rows(rows)
        ui.selected = next(i for i, r in enumerate(rows) if r["type"] == "proc" and r["proc"]["pid"] == 20)
        ui.cycle_sort()
        self.assertEqual(ui.sort, "sm")
        ui.set_rows(tasks.flatten(tree(), sort=ui.sort))
        self.assertEqual(ui.current()["proc"]["pid"], 20)

    def test_filter_typing(self):
        ui = tasks.TaskUI()
        ui.start_filter()
        for ch in "ffx":
            ui.filter_key(ord(ch))
        ui.filter_key(263)                      # backspace
        self.assertEqual(ui.filter, "ff")
        self.assertFalse(ui.filter_key(10))     # Enter keeps it
        self.assertEqual((ui.filter, ui.editing), ("ff", False))
        ui.start_filter()
        ui.filter_key(27)                       # Esc clears it
        self.assertEqual((ui.filter, ui.editing), ("", False))

    def test_q_is_text_while_typing(self):
        ui = tasks.TaskUI()
        ui.start_filter()
        self.assertTrue(ui.filter_key(ord("q")))
        self.assertEqual(ui.filter, "q")

    def test_view_keys_do_nothing_while_confirming(self):
        ui = tasks.TaskUI()
        rows = tasks.flatten(tree())
        ui.set_rows(rows)
        ui.selected = 2
        ui.arm_end()
        ui.cycle_sort()
        ui.toggle_flat()
        ui.start_filter()
        self.assertEqual((ui.sort, ui.flat, ui.editing), ("mem", False, False))


def gsample(procs_by_gpu):
    return [{"id": str(i), "procs": [{"pid": pid, "name": f"p{pid}", "mem": mem, "sm": sm} for pid, mem, sm in procs]}
            for i, procs in enumerate(procs_by_gpu)]


class ProcTrackerTest(unittest.TestCase):
    def tracker(self):
        t = tasks.ProcTracker(maxlen=5)
        t._meta = lambda pid, name: {"name": name, "cmd": f"{name} -x", "user": "ml", "started": 1000.0}
        return t

    def test_history_is_bounded_and_peaks_kept(self):
        t = self.tracker()
        for k in range(8):
            t.update(gsample([[(7, 100 + k, 10 * k)]]), now=2000 + k)
        snap = t.snapshot()["proc"][("0", 7)]
        self.assertEqual(len(snap["sm"]), 5)
        self.assertEqual(snap["sm"][-1], 70)
        self.assertEqual((snap["peak_sm"], snap["peak_mem"]), (70, 107))

    def test_ended_once_with_peaks_and_gpus(self):
        t = self.tracker()
        t.update(gsample([[(7, 100, 90)], [(7, 50, 20)]]), now=2000)
        t.update(gsample([[], [(7, 60, 30)]]), now=2001)
        self.assertEqual(t.snapshot()["ended"], [])          # still on GPU 1
        with mock.patch.object(tasks.psutil, "Process"):
            t._ps.pop(7, None)
            t.update(gsample([[], []]), now=2002)
        ended = t.snapshot()["ended"]
        self.assertEqual(len(ended), 1)
        e = ended[0]
        self.assertEqual((e["pid"], e["gpus"], e["peak_sm"], e["peak_mem"], e["ran"], e["how"]),
                         (7, ["1"], 30, 60, 1002.0, "ended"))

    def test_ended_list_forgets_after_an_hour(self):
        t = self.tracker()
        t.update(gsample([[(7, 100, 90)]]), now=2000)
        t.update(gsample([[]]), now=2001)
        t.update(gsample([[]]), now=2001 + 3601)
        self.assertEqual(t.snapshot()["ended"], [])


class ProcDetailTest(unittest.TestCase):
    def test_only_the_allow_listed_environment(self):
        tasks._detail_cache.clear()
        fake = mock.MagicMock()
        fake.environ.return_value = {"CUDA_VISIBLE_DEVICES": "1", "HF_TOKEN": "secret", "PATH": "/bin"}
        fake.cwd.return_value = "/home/ml/work"
        fake.num_threads.return_value = 3
        fake.ppid.return_value = 0
        fake.create_time.return_value = 5.0
        fake.uids.return_value = mock.Mock(real=4242)
        with mock.patch.object(tasks.psutil, "Process", return_value=fake), \
                mock.patch.object(tasks, "_PROCFS", False):
            d = tasks.proc_detail(123456, now=10.0)
        self.assertEqual(d["env"], [("CUDA_VISIBLE_DEVICES", "1")])
        self.assertNotIn("secret", repr(d))

    def test_home_is_shortened(self):
        self.assertEqual(tasks._home_short("/home/ml/work", "/home/ml"), "~/work")
        self.assertEqual(tasks._home_short("/home/mlx", "/home/ml"), "/home/mlx")


class FabricBusyTest(unittest.TestCase):
    def test_desktop_processes_are_not_a_job(self):
        views = [{"name": "a", "sample": {"gpus": [{"util": 0, "procs": [{"name": "Xorg"}, {"name": "gnome-shell"}]}]}},
                 {"name": "b", "sample": {"gpus": [{"util": 0, "procs": [{"name": "python3"}]}]}}]
        self.assertEqual(fabtest.gpu_active_nodes(views), ["b"])


class PageTest(unittest.TestCase):
    def setUp(self):
        self.style = term.BAR_STYLE
        term.BAR_STYLE = "eighths"

    def tearDown(self):
        term.BAR_STYLE = self.style

    def build(self, w, h, sm=True, ui=True, track=True, layout=None, **view):
        t = tree(sm)
        u = tasks.TaskUI()
        for k, v in view.items():
            setattr(u, k, v)
        rows = tasks.flatten(t, sort=u.sort, flat=u.flat, filt=u.filter)
        u.set_rows(rows)
        u.selected = 2
        tr = None
        if track:
            tr = {"proc": {(str(r["idx"]), p["pid"]): {"sm": [float(p.get("sm") or 0)] * 400,
                                                       "mem": [float(p["gpu_mem"])] * 400}
                           for r in t["gpus"] for g in r["groups"] for p in g["procs"]},
                  "cpu": {10: [50.0] * 400},
                  "ended": [{"pid": 99, "name": "eval", "cmd": "eval --x", "user": "ml", "gpus": ["0"],
                             "ran": 60, "ended": time.time() - 5, "peak_sm": 99, "peak_mem": GiB, "how": "ended"}]}
        tier = pages.tier_for_width(w)
        big = layout in ("large", "xl")
        dw = w if big else pages.content_width(tier, w)
        built = taskpage.build_page5(sample(sm), t, rows, tier, dw, 0, height=h, ui=u.render_ctx() if ui else None,
                                     track=tr, detail={"cwd": "~/w", "env": [], "devices": ["nvidia0"]},
                                     layout=layout)
        return render(built, w, h)

    def test_footer_is_the_last_line_and_nothing_spills(self):
        for w, h in ((80, 30), (120, 40), (120, 46), (160, 50), (100, 24)):
            lines = self.build(w, h)
            last = max(i for i, s in enumerate(lines) if s.strip())
            self.assertIn("select", lines[last], (w, h))
            self.assertTrue(all(len(s) <= w for s in lines))
            self.assertEqual(sum("╭─ PROCESS" in s for s in lines), 1, (w, h))
        # too short for the tree and a detail pane: the tree wins
        self.assertFalse(any("╭─ PROCESS" in s for s in self.build(100, 16)))

    def test_sm_column_only_with_per_process_data(self):
        self.assertIn("SM%", self.build(120, 40)[2])
        self.assertNotIn("SM%", self.build(120, 40, sm=False)[2])

    def test_recently_ended_shows_when_rows_are_free(self):
        self.assertTrue(any("RECENTLY ENDED" in s for s in self.build(120, 46)))

    def test_filter_without_match_says_so(self):
        self.assertTrue(any('no process matches "zzz"' in s for s in self.build(120, 40, filter="zzz")))

    def test_large_layout_fills_the_screen(self):
        lines = self.build(240, 67, layout="large")
        self.assertIn("select", lines[66])
        self.assertTrue(any("SM by process" in s for s in lines))
        self.assertTrue(any("GPU memory by process" in s for s in lines))
        self.assertTrue(any("╭─ PROCESS" in s for s in lines))

    def test_snapshot_reads_only(self):
        lines = self.build(120, 40, ui=False, track=False)
        self.assertTrue(any("read-only" in s for s in lines))
        self.assertFalse(any("▶" in s for s in lines))

    def test_ascii_has_no_braille(self):
        term.BAR_STYLE = "ascii"
        text = "\n".join(self.build(120, 46))
        self.assertFalse(any(0x2800 <= ord(c) <= 0x28FF for c in text))


if __name__ == "__main__":
    unittest.main()
