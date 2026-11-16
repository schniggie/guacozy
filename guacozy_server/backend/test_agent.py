import json

from django.test import Client, TestCase
from django.utils import timezone

from backend.agent.tokens import authenticate_bearer, create_agent_token, hash_token
from backend.models import AgentToken
from users.models import User


class AgentTokenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="agent-owner", password="secret")
        self.other = User.objects.create_user(username="agent-other", password="secret")

    def test_create_lists_hash_and_revoke(self):
        self.client.force_login(self.user)
        created = self.client.post("/api/agent-tokens/", {"name": "laptop"}, content_type="application/json")
        self.assertEqual(created.status_code, 201)
        created_body = created.json()
        self.assertEqual(set(created_body.keys()), {"id", "name", "token", "created"})
        secret = created_body["token"]
        stored = AgentToken.objects.get(name="laptop")
        self.assertEqual(stored.token_hash, hash_token(secret))
        self.assertNotIn(secret, stored.token_hash)

        create_agent_token(self.other, "other-laptop")
        listed_response = self.client.get("/api/agent-tokens/")
        self.assertNotIn(secret, listed_response.content.decode())
        listed = listed_response.json()
        self.assertEqual([row["name"] for row in listed], ["laptop"])
        self.assertNotIn("token", listed[0])
        self.assertEqual(
            set(listed[0].keys()),
            {"id", "name", "created", "last_used_at", "revoked_at"},
        )
        self.assertEqual(authenticate_bearer("Bearer " + secret).username, self.user.username)
        self.assertIsNotNone(AgentToken.objects.get(pk=listed[0]["id"]).last_used_at)
        self.client.post(f"/api/agent-tokens/{listed[0]['id']}/revoke/")
        self.assertIsNotNone(AgentToken.objects.get(pk=listed[0]["id"]).revoked_at)
        self.assertIsNone(authenticate_bearer("Bearer " + secret))

        self.client.force_login(self.other)
        denied = self.client.post(f"/api/agent-tokens/{listed[0]['id']}/revoke/")
        self.assertEqual(denied.status_code, 404)


class AgentMcpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="mcp-owner",
            password="secret",
            is_staff=True,
        )
        self.other = User.objects.create_user(username="mcp-other", password="secret")
        _token, self.secret = create_agent_token(self.user, "laptop")

    def rpc(self, method, params=None):
        response = self.client.post(
            "/mcp/",
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.secret,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["id"], 1)
        return body

    def test_mcp_ignores_session_cookie(self):
        self.client.force_login(self.user)
        response = self.client.post(
            "/mcp/",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["retry"], "never")
        self.assertEqual(response.json()["error"]["code"], "NOT_ALLOWED")
        self.assertEqual(
            response.json()["error"]["message"],
            "Authentication credentials were not provided.",
        )

    def test_whoami_runs_as_the_token_user(self):
        self.client.force_login(self.other)
        body = self.rpc("tools/call", {"name": "guacozy_whoami", "arguments": {}})
        self.assertEqual(body["result"]["isError"], False)
        payload = json.loads(body["result"]["content"][0]["text"])
        self.assertEqual(payload["data"]["username"], self.user.username)
        self.assertEqual(payload["data"]["id"], self.user.pk)
        self.assertEqual(payload["data"]["is_staff"], True)

    def test_initialize_names_the_server(self):
        for version in ("2024-11-05", "2025-03-26", "2025-06-18"):
            with self.subTest(version=version):
                body = self.rpc("initialize", {"protocolVersion": version})
                self.assertEqual(body["result"]["serverInfo"]["name"], "guacozy")
                self.assertEqual(body["result"]["protocolVersion"], version)
                self.assertEqual(body["result"]["capabilities"], {"tools": {}})
        fallback = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(fallback["result"]["protocolVersion"], "2025-03-26")
        omitted = self.rpc("initialize", {})
        self.assertEqual(omitted["result"]["protocolVersion"], "2025-03-26")

    def test_tools_list_names_whoami(self):
        body = self.rpc("tools/list")
        self.assertEqual(
            [tool["name"] for tool in body["result"]["tools"]],
            ["guacozy_whoami"],
        )

    def test_initialized_notification_is_empty(self):
        response = self.client.post(
            "/mcp/",
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.secret,
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_unknown_method_is_not_found(self):
        body = self.rpc("tools/nope")
        self.assertEqual(body["error"]["code"], -32601)
        self.assertNotIn("result", body)

    def test_unknown_tool_is_not_found(self):
        body = self.rpc("tools/call", {"name": "guacozy_missing", "arguments": {}})
        self.assertEqual(body["result"]["isError"], True)
        payload = json.loads(body["result"]["content"][0]["text"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")
        self.assertEqual(payload["error"]["retry"], "never")

    def test_get_is_not_allowed(self):
        response = self.client.get("/mcp/")
        self.assertEqual(response.status_code, 405)

    def test_bad_and_revoked_bearer_are_rejected(self):
        rejected = self.client.post(
            "/mcp/",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer not-a-token",
        )
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(
            rejected.json()["error"],
            {
                "code": "NOT_ALLOWED",
                "message": "Authentication credentials were not provided.",
                "retry": "never",
            },
        )
        token, secret = create_agent_token(self.user, "old")
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        revoked = self.client.post(
            "/mcp/",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + secret,
        )
        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.json()["error"]["retry"], "never")

    def test_bearer_does_not_need_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/mcp/",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.secret,
        )
        self.assertEqual(response.status_code, 200)
