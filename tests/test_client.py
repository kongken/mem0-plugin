import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from mem0_oss import client as client_mod
from tests.fake_server import FakeMem0Server


class ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeMem0Server().start()
        self.addCleanup(self.server.stop)

    def make_client(self, api_key="m0sk_testkey", timeout_ms=5000):
        return client_mod.Client(self.server.base_url, api_key, timeout_ms)


class TestNormalizeResults(unittest.TestCase):
    def test_results_envelope(self):
        self.assertEqual(
            client_mod.normalize_results({"results": [{"id": "1"}]}), [{"id": "1"}]
        )

    def test_bare_list(self):
        self.assertEqual(client_mod.normalize_results([{"id": "1"}]), [{"id": "1"}])

    def test_non_dict_entries_are_dropped(self):
        self.assertEqual(client_mod.normalize_results({"results": ["x", {"id": "1"}]}), [{"id": "1"}])

    def test_garbage_becomes_empty(self):
        for payload in (None, "hello", 42, {"nope": 1}, {"results": "nope"}):
            self.assertEqual(client_mod.normalize_results(payload), [], repr(payload))


class TestSearchWire(ClientTestCase):
    def test_path_and_method(self):
        self.make_client().search("查询", "alice:project:abc123def456")
        req = self.server.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/search")

    def test_body_shape(self):
        self.make_client().search("查询", "alice", top_k=7)
        body = self.server.requests[0]["body"]
        self.assertEqual(body["query"], "查询")
        self.assertEqual(body["filters"], {"user_id": "alice"})
        self.assertEqual(body["top_k"], 7)

    def test_agent_id_goes_into_filters(self):
        self.make_client().search("q", "alice", agent_id="agent-1")
        self.assertEqual(
            self.server.requests[0]["body"]["filters"],
            {"user_id": "alice", "agent_id": "agent-1"},
        )

    def test_agent_id_omitted_when_blank(self):
        self.make_client().search("q", "alice", agent_id="")
        self.assertNotIn("agent_id", self.server.requests[0]["body"]["filters"])

    def test_api_key_header(self):
        self.make_client(api_key="m0sk_abc").search("q", "alice")
        self.assertEqual(self.server.requests[0]["headers"]["X-API-Key"], "m0sk_abc")

    def test_no_auth_header_when_key_absent(self):
        self.make_client(api_key="").search("q", "alice")
        self.assertNotIn("X-API-Key", self.server.requests[0]["headers"])

    def test_returns_normalized_results(self):
        self.server.set_response(
            "POST", "/search", 200, {"results": [{"id": "1", "memory": "hi"}]}
        )
        got = self.make_client().search("q", "alice")
        self.assertEqual(got, [{"id": "1", "memory": "hi"}])


class TestAddWire(ClientTestCase):
    def test_path_and_body(self):
        messages = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "回答"},
        ]
        self.make_client().add(messages, "alice", metadata={"source": "claude-code"})
        req = self.server.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/memories")
        self.assertEqual(req["body"]["messages"], messages)
        self.assertEqual(req["body"]["user_id"], "alice")
        self.assertEqual(req["body"]["metadata"], {"source": "claude-code"})

    def test_infer_defaults_true(self):
        # 服务端靠 infer 抽事实,关掉就退化成流水账
        self.make_client().add([{"role": "user", "content": "x"}], "alice")
        self.assertIs(self.server.requests[0]["body"]["infer"], True)

    def test_metadata_omitted_when_empty(self):
        self.make_client().add([{"role": "user", "content": "x"}], "alice")
        self.assertNotIn("metadata", self.server.requests[0]["body"])


class TestGetAllWire(ClientTestCase):
    def test_path_and_query(self):
        self.make_client().get_all("alice:project:abc")
        req = self.server.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertTrue(req["path"].startswith("/memories?"))
        self.assertIn("user_id=alice%3Aproject%3Aabc", req["path"])

    def test_agent_id_in_query(self):
        self.make_client().get_all("alice", agent_id="agent-1")
        self.assertIn("agent_id=agent-1", self.server.requests[0]["path"])


class TestDeleteWire(ClientTestCase):
    def test_path_and_method(self):
        self.make_client().delete("mem-123")
        req = self.server.requests[0]
        self.assertEqual(req["method"], "DELETE")
        self.assertEqual(req["path"], "/memories/mem-123")

    def test_memory_id_is_url_quoted(self):
        self.make_client().delete("a/b c")
        self.assertEqual(self.server.requests[0]["path"], "/memories/a%2Fb%20c")


class TestErrors(ClientTestCase):
    def test_401_carries_status(self):
        self.server.set_response("POST", "/search", 401, {"detail": "nope"})
        with self.assertRaises(client_mod.Mem0Error) as ctx:
            self.make_client().search("q", "alice")
        self.assertEqual(ctx.exception.status, 401)

    def test_403_carries_status(self):
        self.server.set_response("POST", "/search", 403, {"detail": "nope"})
        with self.assertRaises(client_mod.Mem0Error) as ctx:
            self.make_client().search("q", "alice")
        self.assertEqual(ctx.exception.status, 403)

    def test_500_raises(self):
        self.server.set_response("POST", "/search", 500, {"detail": "boom"})
        with self.assertRaises(client_mod.Mem0Error):
            self.make_client().search("q", "alice")

    def test_non_json_body_raises(self):
        self.server.set_response("POST", "/search", 200, "not json at all")
        with self.assertRaises(client_mod.Mem0Error):
            self.make_client().search("q", "alice")

    def test_timeout_raises(self):
        self.server.delay_seconds = 1.0
        with self.assertRaises(client_mod.Mem0Error):
            self.make_client(timeout_ms=100).search("q", "alice")

    def test_unreachable_host_raises(self):
        api = client_mod.Client("http://127.0.0.1:1", "", 500)
        with self.assertRaises(client_mod.Mem0Error):
            api.search("q", "alice")


class TestBaseUrl(ClientTestCase):
    def test_trailing_slash_stripped(self):
        api = client_mod.Client(self.server.base_url + "/", "", 5000)
        api.search("q", "alice")
        self.assertEqual(self.server.requests[0]["path"], "/search")


if __name__ == "__main__":
    unittest.main()
