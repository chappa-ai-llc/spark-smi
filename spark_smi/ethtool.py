"""Physical-port byte counters through ethtool's netlink interface
(ETHTOOL_MSG_STATS_GET, the "eth-mac" group of IEEE 802.3 MAC counters,
what `ethtool -S IFACE --groups eth-mac` prints), with the standard
library only.

Why (issue #11): on some NICs the kernel's netdev counters miss RDMA
traffic and the HCA's port counters miss kernel traffic. The MAC counters
OctetsReceivedOK / OctetsTransmittedOK count every frame that crosses the
port, whichever path sent it. Reading them needs no root.

Linux 5.13 or later, and a driver that reports the MAC counters (mlx5 does;
checked on a ConnectX-7). Anywhere else port_byte_reader() returns None and
the caller keeps its other sources.

Constants are from <linux/ethtool_netlink.h> and <linux/genetlink.h>.
"""
import socket
import struct
import threading

NETLINK_GENERIC = 16
GENL_ID_CTRL = 0x10
CTRL_CMD_GETFAMILY = 3
CTRL_ATTR_FAMILY_ID = 1
CTRL_ATTR_FAMILY_NAME = 2
NLMSG_ERROR = 2
NLM_F_REQUEST = 1
NLA_F_NESTED = 0x8000
NLA_TYPE_MASK = 0x3FFF

ETHTOOL_GENL_NAME = b"ethtool"
ETHTOOL_MSG_STATS_GET = 32
ETHTOOL_MSG_STATS_GET_REPLY = 33
ETHTOOL_A_STATS_HEADER = 2
ETHTOOL_A_STATS_GROUPS = 3
ETHTOOL_A_STATS_GRP = 4
ETHTOOL_A_HEADER_DEV_NAME = 2
ETHTOOL_A_BITSET_NOMASK = 1
ETHTOOL_A_BITSET_SIZE = 2
ETHTOOL_A_BITSET_VALUE = 4
ETHTOOL_A_STATS_GRP_ID = 2
ETHTOOL_A_STATS_GRP_STAT = 4
ETHTOOL_STATS_ETH_MAC = 1
ETHTOOL_A_STATS_ETH_MAC_8_TX_BYTES = 6
ETHTOOL_A_STATS_ETH_MAC_14_RX_BYTES = 12

IFNAMSIZ = 16
_TIMEOUT = 0.5  # seconds; the render loop must never wait long on this


def _attr(atype, payload):
    """One netlink attribute, padded to 4 bytes."""
    hdr = struct.pack("=HH", 4 + len(payload), atype)
    pad = b"\0" * (-len(payload) % 4)
    return hdr + payload + pad


def _attrs(buf, off=0, end=None):
    """Yield (type, payload) for each attribute in buf[off:end]."""
    end = len(buf) if end is None else end
    while off + 4 <= end:
        alen, atype = struct.unpack_from("=HH", buf, off)
        if alen < 4 or off + alen > end:
            return
        yield atype & NLA_TYPE_MASK, buf[off + 4:off + alen]
        off += (alen + 3) & ~3


def stats_request(iface):
    """The ETHTOOL_MSG_STATS_GET attributes asking iface for the eth-mac
    group (a compact, mask-less bitset with that one bit set)."""
    header = _attr(ETHTOOL_A_HEADER_DEV_NAME, iface.encode() + b"\0")
    groups = (_attr(ETHTOOL_A_BITSET_NOMASK, b"")
              + _attr(ETHTOOL_A_BITSET_SIZE, struct.pack("=I", 32))
              + _attr(ETHTOOL_A_BITSET_VALUE, struct.pack("=I", 1 << ETHTOOL_STATS_ETH_MAC)))
    return (_attr(ETHTOOL_A_STATS_HEADER | NLA_F_NESTED, header)
            + _attr(ETHTOOL_A_STATS_GROUPS | NLA_F_NESTED, groups))


def parse_mac_bytes(attrs):
    """(rx_bytes, tx_bytes) from a STATS_GET_REPLY's attributes, or None
    when the driver did not report both. A driver leaves out counters it
    does not have, so absence means "unknown", never 0."""
    found = {}
    for atype, grp in _attrs(attrs):
        if atype != ETHTOOL_A_STATS_GRP:
            continue
        stats, grp_id = {}, None
        for gtype, gval in _attrs(grp):
            if gtype == ETHTOOL_A_STATS_GRP_ID and len(gval) >= 4:
                grp_id = struct.unpack_from("=I", gval)[0]
            elif gtype == ETHTOOL_A_STATS_GRP_STAT:
                # each counter is its own nest: one attr, type = counter id
                for stype, sval in _attrs(gval):
                    if len(sval) == 8:
                        stats[stype] = struct.unpack("=Q", sval)[0]
        if grp_id == ETHTOOL_STATS_ETH_MAC:
            found = stats
    rx = found.get(ETHTOOL_A_STATS_ETH_MAC_14_RX_BYTES)
    tx = found.get(ETHTOOL_A_STATS_ETH_MAC_8_TX_BYTES)
    if rx is None or tx is None:
        return None
    return (rx, tx)


class _Genl:
    """One generic-netlink socket for the process, with the ethtool family
    id resolved once. Requests are serialized (the --serve agent and the
    render loop may sample from different threads)."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
        self.sock.settimeout(_TIMEOUT)
        self.sock.bind((0, 0))
        self.lock = threading.Lock()
        self.seq = 0
        reply = self.request(GENL_ID_CTRL, CTRL_CMD_GETFAMILY,
                             _attr(CTRL_ATTR_FAMILY_NAME, ETHTOOL_GENL_NAME + b"\0"))
        self.family = None
        for atype, val in _attrs(reply or b""):
            if atype == CTRL_ATTR_FAMILY_ID and len(val) >= 2:
                self.family = struct.unpack_from("=H", val)[0]
        if self.family is None:
            raise OSError("no ethtool netlink family")

    def request(self, msg_type, cmd, payload):
        """Send one request; return the reply's attribute bytes, or None on
        a netlink error. Raises OSError (incl. timeout) on socket trouble."""
        with self.lock:
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            seq = self.seq
            body = struct.pack("=BBH", cmd, 1, 0) + payload
            self.sock.send(struct.pack("=IHHII", 16 + len(body), msg_type,
                                       NLM_F_REQUEST, seq, 0) + body)
            while True:
                data = self.sock.recv(65536)
                off = 0
                while off + 16 <= len(data):
                    mlen, mtype, _flags, mseq, _pid = struct.unpack_from("=IHHII", data, off)
                    if mlen < 16:
                        break
                    if mseq == seq:
                        if mtype == NLMSG_ERROR:
                            return None
                        return data[off + 20:off + mlen]  # past nlmsghdr + genlmsghdr
                    off += (mlen + 3) & ~3


_genl = None
_genl_failed = False


def _channel():
    global _genl, _genl_failed
    if _genl is None and not _genl_failed:
        try:
            _genl = _Genl()
        except Exception:
            _genl_failed = True  # no netlink, no ethtool family: stop trying
    return _genl


class PortByteReader:
    """Reads one interface's MAC byte counters."""

    def __init__(self, iface, channel):
        self.iface = iface
        self._ch = channel

    def read(self):
        """(rx_bytes, tx_bytes) since the counters' reset, or None when
        unavailable this time. Never raises."""
        try:
            reply = self._ch.request(self._ch.family, ETHTOOL_MSG_STATS_GET,
                                     stats_request(self.iface))
            return parse_mac_bytes(reply) if reply is not None else None
        except Exception:
            return None


def port_byte_reader(iface):
    """A PortByteReader for iface, or None when this kernel, driver or
    platform cannot report its MAC byte counters."""
    if len(iface.encode()) >= IFNAMSIZ or not hasattr(socket, "AF_NETLINK"):
        return None
    ch = _channel()
    if ch is None:
        return None
    reader = PortByteReader(iface, ch)
    return reader if reader.read() is not None else None
