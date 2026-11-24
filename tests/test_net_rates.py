"""NetCollector byte-counter sources and the page-1 caption (issues #10, #11).

Needs psutil (spark_smi.collectors imports it)."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import collectors, pages  # noqa: E402

NC = collectors.NetCollector


def _bare_collector(rdma_counters):
    """A NetCollector without hardware discovery: only what
    _read_byte_counters() touches."""
    nc = NC.__new__(NC)
    nc.rdma_counters = rdma_counters
    nc.caps = {"rdma": bool(rdma_counters), "rdma_counting": False}
    return nc


class RatesTest(unittest.TestCase):
    def test_working_hca_wins_over_netdev(self):
        prev = ((0, 0), (0, 0))
        cur = ((1000, 500), (800, 400))          # HCA counts kernel traffic too
        self.assertEqual(NC._rates(cur, prev, 1.0), (8000.0, 4000.0))

    def test_stuck_hca_falls_back_to_netdev(self):
        prev = ((0, 0), (10_000, 2_000))
        cur = ((0, 0), (1_260_000, 52_000))       # HCA never moves
        self.assertEqual(NC._rates(cur, prev, 1.0), (10_000_000.0, 400_000.0))

    def test_rdma_only_traffic_still_counts(self):
        prev = ((0, 0), (5, 5))
        cur = ((4_000, 4_000), (5, 5))            # RDMA bypasses the kernel
        self.assertEqual(NC._rates(cur, prev, 2.0), (16_000.0, 16_000.0))

    def test_counter_reset_clamps_to_zero(self):
        prev = ((9_000, 9_000), (9_000, 9_000))
        cur = ((10, 10), (10, 10))
        self.assertEqual(NC._rates(cur, prev, 1.0), (0.0, 0.0))

    def test_missing_source_is_ignored(self):
        prev = (None, (0, 0))
        cur = (None, (125, 250))
        self.assertEqual(NC._rates(cur, prev, 1.0), (1000.0, 2000.0))


class ReadCountersTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _hca(self, rx_words, tx_words):
        paths = []
        for name, v in (("port_rcv_data", rx_words), ("port_xmit_data", tx_words)):
            p = os.path.join(self.dir.name, name)
            with open(p, "w") as f:
                f.write(f"{v}\n")
            paths.append(p)
        return tuple(paths)

    def test_reads_both_sources_and_scales_words(self):
        nc = _bare_collector({"eth0": self._hca(10, 20)})
        stats = {"eth0": SimpleNamespace(bytes_recv=7, bytes_sent=9)}
        self.assertEqual(nc._read_byte_counters("eth0", stats), ((40, 80), (7, 9)))
        self.assertTrue(nc.caps["rdma_counting"])

    def test_zero_hca_leaves_counting_flag_off(self):
        nc = _bare_collector({"eth0": self._hca(0, 0)})
        stats = {"eth0": SimpleNamespace(bytes_recv=7, bytes_sent=9)}
        self.assertEqual(nc._read_byte_counters("eth0", stats), ((0, 0), (7, 9)))
        self.assertFalse(nc.caps["rdma_counting"])

    def test_no_sources_is_none(self):
        nc = _bare_collector({})
        self.assertIsNone(nc._read_byte_counters("no-such-nic-xyz", {}))


class FakeReader:
    def __init__(self, *readings):
        self.readings = list(readings)

    def read(self):
        return self.readings.pop(0) if self.readings else None


class PortCountersTest(unittest.TestCase):
    """Physical-port MAC counters (issue #11)."""

    def test_mixed_traffic_reads_the_port_total(self):
        rate = {"a": (100.0, 50.0)}              # per-netdev rule: the larger source only
        port = {"a": (900.0, 800.0)}             # MAC counter: TCP + RDMA
        self.assertEqual(NC._combine(["a"], {"a": "a"}, rate, port), (900.0, 800.0))

    def test_shared_port_counts_once(self):
        unit = {"pf0": ("sw", "p0"), "pf1": ("sw", "p0")}
        rate = {"pf0": (10.0, 10.0), "pf1": (20.0, 5.0)}
        port = {("sw", "p0"): (100.0, 40.0)}     # both PFs read this same counter
        self.assertEqual(NC._combine(["pf0", "pf1"], unit, rate, port), (100.0, 40.0))

    def test_per_netdev_sum_is_the_floor(self):
        unit = {"pf0": ("sw", "p0"), "pf1": ("sw", "p0")}
        rate = {"pf0": (60.0, 0.0), "pf1": (60.0, 0.0)}
        port = {("sw", "p0"): (0.0, 0.0)}        # a MAC counter that does not move
        self.assertEqual(NC._combine(["pf0", "pf1"], unit, rate, port), (120.0, 0.0))

    def test_ports_without_mac_counters_sum_as_before(self):
        unit = {"a": "a", "b": "b"}
        rate = {"a": (1.0, 2.0), "b": (3.0, 4.0)}
        self.assertEqual(NC._combine(["a", "b"], unit, rate, {}), (4.0, 6.0))

    def test_port_rate_deltas_and_reset(self):
        nc = NC.__new__(NC)
        nc.port_readers = {"u": FakeReader((1_000, 2_000), (500, 2_250), None)}
        prev = {"u": (0, 0)}
        self.assertEqual(nc._port_rate("u", prev, 1.0), (8_000.0, 16_000.0))
        self.assertEqual(nc._port_rate("u", prev, 1.0), (0.0, 2_000.0))  # rx reset clamps
        self.assertIsNone(nc._port_rate("u", prev, 1.0))                 # read failed
        self.assertEqual(prev["u"], (500, 2_250))

    def test_page2_uses_only_single_pf_ports(self):
        nc = NC.__new__(NC)
        nc._rdma_dev_map = {"pf0": ("mlx5_0", "1"), "pf1": ("mlx5_1", "1"), "solo": ("mlx5_2", "1")}
        nc._unit = {"pf0": ("sw", "p0"), "pf1": ("sw", "p0"), "solo": "solo"}
        nc._unit_size = {("sw", "p0"): 2, "solo": 1}
        nc.port_readers = {("sw", "p0"): FakeReader(), "solo": FakeReader()}
        self.assertEqual(nc._pf_port_units(), ["solo"])


class CaptionTest(unittest.TestCase):
    def _caption(self, **kw):
        p = pages._build_network_panel(0, 0, 120, [], False, "wide", **kw)
        return "".join(t for t, _ in p.title)

    def test_counting(self):
        self.assertIn("RDMA read from HCA counters",
                      self._caption(has_rdma=True, rdma_counting=True))

    def test_stuck_counters_say_so(self):
        self.assertIn("HCA counters read 0, kernel traffic only",
                      self._caption(has_rdma=True, rdma_counting=False))

    def test_port_counters_count_everything(self):
        self.assertIn("port counters, RDMA + kernel",
                      self._caption(has_rdma=True, rdma_counting=False, port_counting=True))

    def test_no_rdma_device(self):
        self.assertEqual(self._caption(has_rdma=False).strip(), "NETWORK physical ports")


if __name__ == "__main__":
    unittest.main()
