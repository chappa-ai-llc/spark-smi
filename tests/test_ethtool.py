"""spark_smi.ethtool: the netlink request it builds and the replies it
parses (issue #11). Needs no NIC: replies are built here, byte for byte."""
import os
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import ethtool as et  # noqa: E402


def attr(atype, payload):
    return et._attr(atype, payload)


def stat(counter, value):
    """One ETHTOOL_A_STATS_GRP_STAT nest holding one u64 counter."""
    return attr(et.ETHTOOL_A_STATS_GRP_STAT | et.NLA_F_NESTED,
                attr(counter, struct.pack("=Q", value)))


def group(grp_id, *stats, pad=False):
    body = attr(et.ETHTOOL_A_STATS_GRP_ID, struct.pack("=I", grp_id))
    if pad:
        body += attr(1, b"")  # ETHTOOL_A_STATS_GRP_PAD, as on strict-alignment CPUs
    return attr(et.ETHTOOL_A_STATS_GRP | et.NLA_F_NESTED, body + b"".join(stats))


class AttrTest(unittest.TestCase):
    def test_padding_and_walk(self):
        buf = attr(5, b"abc") + attr(6, b"") + attr(7, b"12345")
        self.assertEqual(len(buf), 8 + 4 + 12)
        self.assertEqual(list(et._attrs(buf)), [(5, b"abc"), (6, b""), (7, b"12345")])

    def test_nested_flag_is_masked(self):
        buf = attr(4 | et.NLA_F_NESTED, b"xxxx")
        self.assertEqual(list(et._attrs(buf)), [(4, b"xxxx")])

    def test_truncated_attr_stops(self):
        buf = attr(5, b"abcd")[:6]
        self.assertEqual(list(et._attrs(buf)), [])


class RequestTest(unittest.TestCase):
    def test_asks_for_eth_mac_group_by_name(self):
        req = dict(et._attrs(et.stats_request("enp1s0f0np0")))
        header = dict(et._attrs(req[et.ETHTOOL_A_STATS_HEADER]))
        self.assertEqual(header[et.ETHTOOL_A_HEADER_DEV_NAME], b"enp1s0f0np0\0")
        groups = dict(et._attrs(req[et.ETHTOOL_A_STATS_GROUPS]))
        self.assertEqual(groups[et.ETHTOOL_A_BITSET_NOMASK], b"")
        self.assertEqual(struct.unpack("=I", groups[et.ETHTOOL_A_BITSET_SIZE])[0], 32)
        self.assertEqual(struct.unpack("=I", groups[et.ETHTOOL_A_BITSET_VALUE])[0], 0b10)

    def test_nests_carry_the_nested_flag(self):
        raw = et.stats_request("eth0")
        flags = [struct.unpack_from("=HH", raw, 0)[1]]
        flags.append(struct.unpack_from("=HH", raw, (struct.unpack_from("=H", raw, 0)[0] + 3) & ~3)[1])
        self.assertTrue(all(f & et.NLA_F_NESTED for f in flags))


class ParseTest(unittest.TestCase):
    TX, RX = et.ETHTOOL_A_STATS_ETH_MAC_8_TX_BYTES, et.ETHTOOL_A_STATS_ETH_MAC_14_RX_BYTES

    def test_reads_rx_and_tx_octets(self):
        reply = (attr(et.ETHTOOL_A_STATS_HEADER | et.NLA_F_NESTED, b"")
                 + group(et.ETHTOOL_STATS_ETH_MAC,
                         stat(0, 210483780), stat(self.TX, 1814462653805),
                         stat(self.RX, 771166083)))
        self.assertEqual(et.parse_mac_bytes(reply), (771166083, 1814462653805))

    def test_ignores_other_groups(self):
        reply = (group(0, stat(self.TX, 1), stat(self.RX, 2))       # eth-phy
                 + group(et.ETHTOOL_STATS_ETH_MAC, stat(self.TX, 30), stat(self.RX, 40), pad=True))
        self.assertEqual(et.parse_mac_bytes(reply), (40, 30))

    def test_counter_left_out_is_unknown_not_zero(self):
        reply = group(et.ETHTOOL_STATS_ETH_MAC, stat(self.TX, 30))
        self.assertIsNone(et.parse_mac_bytes(reply))

    def test_driver_without_mac_stats(self):
        self.assertIsNone(et.parse_mac_bytes(attr(et.ETHTOOL_A_STATS_HEADER, b"")))


class FakeSock:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def recv(self, _n):
        if not self.replies:
            raise TimeoutError("timed out")
        return self.replies.pop(0)


def nlmsg(mtype, seq, attrs=b"", cmd=0):
    body = struct.pack("=BBH", cmd, 1, 0) + attrs
    return struct.pack("=IHHII", 16 + len(body), mtype, 0, seq, 0) + body


class GenlTest(unittest.TestCase):
    def _genl(self, replies):
        g = et._Genl.__new__(et._Genl)
        g.sock = FakeSock(replies)
        g.lock = threading.Lock()
        g.seq = 0
        g.family = 29
        return g

    def test_skips_stale_reply_and_returns_ours(self):
        mine = group(et.ETHTOOL_STATS_ETH_MAC, stat(6, 5), stat(12, 7))
        g = self._genl([nlmsg(29, 99, b"old"), nlmsg(29, 1, mine)])
        reply = g.request(29, et.ETHTOOL_MSG_STATS_GET, et.stats_request("eth0"))
        self.assertEqual(et.parse_mac_bytes(reply), (7, 5))
        mtype, flags, seq = struct.unpack_from("=HHI", g.sock.sent[0], 4)
        self.assertEqual((mtype, flags, seq), (29, et.NLM_F_REQUEST, 1))
        self.assertEqual(g.sock.sent[0][16], et.ETHTOOL_MSG_STATS_GET)

    def test_netlink_error_is_none(self):
        g = self._genl([nlmsg(et.NLMSG_ERROR, 1, struct.pack("=i", -95))])
        self.assertIsNone(g.request(29, et.ETHTOOL_MSG_STATS_GET, b""))

    def test_reader_never_raises(self):
        g = self._genl([])                       # recv times out
        self.assertIsNone(et.PortByteReader("eth0", g).read())

    def test_long_name_has_no_reader(self):
        self.assertIsNone(et.port_byte_reader("x" * 16))


if __name__ == "__main__":
    unittest.main()
