"""NetCollector byte-counter sources and the page-1 caption (issue #10).

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

    def test_no_rdma_device(self):
        self.assertEqual(self._caption(has_rdma=False).strip(), "NETWORK physical ports")


if __name__ == "__main__":
    unittest.main()
