"""Display-name override: --node-name / $SPARK_SMI_NODE_NAME (issue #9)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spark_smi import app, cluster, fabtest, node  # noqa: E402


class _FakeState:
    def sample(self):
        return {"cpu": {}, "gpus": []}


class ValidateTest(unittest.TestCase):
    def test_accepts_hostname_labels(self):
        for name in ("node-1", "a", "Spark_A", "x" * 63, " node-2 "):
            self.assertEqual(node.validate(name), name.strip())

    def test_rejects_unsafe_or_ambiguous_names(self):
        for name in ("", " ", "-node", "node-", "a.b", "has space", "x" * 64,
                     "bad\x1b[31m", "tab\there", "ünï"):
            with self.assertRaises(ValueError, msg=repr(name)):
                node.validate(name)


class ResolveTest(unittest.TestCase):
    def test_neither_set_means_none(self):
        self.assertIsNone(node.resolve(None, {}))

    def test_empty_env_counts_as_unset(self):
        self.assertIsNone(node.resolve(None, {node.ENV_VAR: "  "}))

    def test_env_is_used(self):
        self.assertEqual(node.resolve(None, {node.ENV_VAR: "node-1"}), "node-1")

    def test_flag_wins_over_env(self):
        self.assertEqual(node.resolve("node-2", {node.ENV_VAR: "node-1"}), "node-2")

    def test_bad_values_raise(self):
        with self.assertRaises(ValueError):
            node.resolve(None, {node.ENV_VAR: "a.b"})
        with self.assertRaises(ValueError):
            node.resolve("", {})


class ParseArgsTest(unittest.TestCase):
    def test_flag_takes_next_argument(self):
        self.assertEqual(app._parse_args(["--node-name", "node-1", "-l"])["node_name"], "node-1")

    def test_missing_value_is_kept_for_main_to_reject(self):
        self.assertEqual(app._parse_args(["--node-name"])["node_name"], "")

    def test_default_is_none(self):
        self.assertIsNone(app._parse_args([])["node_name"])


class DisplayNameTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(node.set_display_name, None)
        patcher = mock.patch("platform.node", return_value="realhost.example.com")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unset_shows_short_hostname(self):
        node.set_display_name(None)
        self.assertEqual(node.display_name(), "realhost")
        self.assertEqual(node.display_name(fallback="realhost.example.com"), "realhost.example.com")

    def test_override_shows_everywhere_but_hostname(self):
        node.set_display_name("node-1")
        self.assertEqual(node.display_name(), "node-1")
        self.assertEqual(node.display_name(fallback="x"), "node-1")
        self.assertEqual(node.hostname(), "realhost")
        self.assertEqual(cluster.local_hostname(), "realhost")

    def test_payload_carries_display_name(self):
        node.set_display_name("node-1")
        with mock.patch.object(cluster, "machine_model", return_value="m"):
            payload = cluster.build_json_payload(_FakeState())
        self.assertEqual(payload["node"], "node-1")

    def _aggregator(self):
        entries = [{"host": "realhost.example.com", "kind": "http", "port": 8817},
                   {"host": "other.example.com", "kind": "http", "port": 8817}]
        agg = cluster.ClusterAggregator(entries, _FakeState())
        self.addCleanup(agg.stop)
        return agg

    def test_local_member_still_found_by_real_hostname(self):
        node.set_display_name("node-1")
        agg = self._aggregator()
        self.assertEqual([m.is_local for m in agg.members], [True, False])

    def test_local_view_and_lookups_use_display_name(self):
        node.set_display_name("node-1")
        agg = self._aggregator()
        local, remote = agg.members
        cluster._poll_local(local, agg.local_state)
        remote.set_ok({"node": "node-2", "cpu": {}, "gpus": []})
        names = [v["name"] for v in agg.get_views()]
        self.assertEqual(names, ["node-1", "node-2"])
        self.assertIsNotNone(agg.get_member_sample("node-1"))
        # fabtest dials the cluster-list host, never the display name ...
        self.assertEqual(fabtest._member_host(agg, "node-2"), "other.example.com")
        host = fabtest._member_host(agg, "node-1")
        self.assertEqual(host, "realhost.example.com")
        # ... and still runs the local side without ssh
        self.assertTrue(fabtest._is_local(host))

    def test_unset_local_view_name_unchanged(self):
        node.set_display_name(None)
        agg = self._aggregator()
        cluster._poll_local(agg.members[0], agg.local_state)
        self.assertEqual(agg.get_views()[0]["name"], "realhost")

    def test_local_poll_does_not_mutate_state_sample(self):
        shared = {"cpu": {}}

        class _S:
            def sample(self):
                return shared

        node.set_display_name("node-1")
        member = cluster.ClusterMember({"host": "realhost", "kind": "http", "port": 8817}, True)
        cluster._poll_local(member, _S())
        self.assertNotIn("node", shared)


if __name__ == "__main__":
    unittest.main()
