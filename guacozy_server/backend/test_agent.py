import json

from django.test import Client, TestCase
from django.utils import timezone

from backend.agent.tokens import authenticate_bearer, create_agent_token, hash_token
from backend.models import (
    AgentToken,
    ConnectionRdp,
    ConnectionSsh,
    ConnectionVnc,
    Folder,
    FolderPermission,
)
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
            [
                "guacozy_whoami",
                "guacozy_list_folders",
                "guacozy_create_folder",
                "guacozy_update_folder",
                "guacozy_delete_folder",
                "guacozy_list_connections",
                "guacozy_get_connection",
                "guacozy_create_connection",
                "guacozy_update_connection",
                "guacozy_delete_connection",
            ],
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


class AgentManagementTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="mgmt-staff",
            password="secret",
            is_staff=True,
        )
        self.member = User.objects.create_user(
            username="mgmt-member",
            password="secret",
            is_staff=False,
        )
        self.outsider = User.objects.create_user(
            username="mgmt-outsider",
            password="secret",
            is_staff=False,
        )
        self.folder = Folder.objects.create(name="Lab")
        FolderPermission.objects.create(folder=self.folder, user=self.staff)
        FolderPermission.objects.create(folder=self.folder, user=self.member)
        self.visible = ConnectionRdp.objects.create(
            name="VISIBLE",
            host="10.0.0.4",
            parent=self.folder,
            password="s3cret",
        )
        _token, self.staff_secret = create_agent_token(self.staff, "staff-laptop")
        _token, self.member_secret = create_agent_token(self.member, "member-laptop")
        _token, self.outsider_secret = create_agent_token(self.outsider, "outsider-laptop")

    def tool(self, name, arguments):
        return self.tool_as(self.staff, name, arguments)

    def tool_as(self, user, name, arguments):
        secret = {
            self.staff.pk: self.staff_secret,
            self.member.pk: self.member_secret,
            self.outsider.pk: self.outsider_secret,
        }[user.pk]
        response = self.client.post(
            "/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + secret,
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        body = response.json()
        self.assertIn("result", body, body)
        return json.loads(body["result"]["content"][0]["text"])

    def test_connection_payload_omits_secrets(self):
        with self.assertLogs("backend.agent.management", level="INFO") as captured:
            payload = self.tool("guacozy_create_connection", {
                "protocol": "rdp", "name": "WINDOWS10", "host": "10.0.0.5",
                "parent": self.folder.id, "password": "s3cret", "private_key": "nope",
            })
            self.assertTrue(payload["ok"], payload)
            self.assertNotIn("password", payload["data"])
            self.assertNotIn("private_key", payload["data"])
            self.assertNotIn("passphrase", payload["data"])
            self.assertTrue(payload["data"]["has_password"])
            self.assertFalse(payload["data"]["has_private_key"])
            self.assertEqual(
                set(payload["data"]),
                {
                    "id", "name", "protocol", "parent", "host", "port", "username",
                    "domain", "passthrough_credentials", "has_password",
                    "has_private_key", "guacdserver", "ignore_cert", "security",
                    "disable_auth", "console", "initial_program", "server_layout",
                    "color_depth", "resize_method",
                },
            )
            self.assertEqual(payload["data"]["name"], "WINDOWS10")
            self.assertEqual(payload["data"]["protocol"], "rdp")
            self.assertEqual(payload["data"]["parent"], self.folder.id)
            self.assertEqual(payload["data"]["host"], "10.0.0.5")
            self.assertEqual(payload["data"]["port"], 3389)
            stored = ConnectionRdp.objects.get(name="WINDOWS10")
            self.assertEqual(stored.password, "s3cret")
            self.assertEqual(payload["data"]["id"], str(stored.id))
            logged = "\n".join(captured.output)
            self.assertNotIn("s3cret", logged)
            self.assertNotIn("nope", logged)
            self.assertIn(f"guacozy_create_connection {stored.id}", logged)

    def test_non_staff_cannot_create_connection(self):
        payload = self.tool_as(self.member, "guacozy_create_connection", {
            "protocol": "vnc", "name": "screen", "host": "10.0.0.6", "parent": self.folder.id,
        })
        self.assertEqual(payload["error"]["code"], "NOT_ALLOWED")
        self.assertEqual(payload["error"]["retry"], "never")
        self.assertFalse(ConnectionVnc.objects.filter(name="screen").exists())

    def test_hidden_connection_is_not_found(self):
        payload = self.tool_as(self.outsider, "guacozy_get_connection", {"connection_id": str(self.visible.id)})
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def test_blank_password_keeps_the_old_one(self):
        payload = self.tool("guacozy_update_connection", {
            "connection_id": str(self.visible.id),
            "password": "",
        })
        self.assertTrue(payload["ok"], payload)
        self.assertNotIn("password", payload["data"])
        self.assertEqual(ConnectionRdp.objects.get(pk=self.visible.id).password, "s3cret")

    def test_nonblank_password_replaces_the_old_one(self):
        with self.assertLogs("backend.agent.management", level="INFO") as captured:
            payload = self.tool("guacozy_update_connection", {
                "connection_id": str(self.visible.id),
                "password": "new-secret",
            })
            self.assertTrue(payload["ok"], payload)
            self.assertNotIn("password", payload["data"])
            self.assertNotIn("new-secret", json.dumps(payload))
            self.assertTrue(payload["data"]["has_password"])
            self.assertEqual(ConnectionRdp.objects.get(pk=self.visible.id).password, "new-secret")
            logged = "\n".join(captured.output)
            self.assertNotIn("new-secret", logged)
            self.assertNotIn("s3cret", logged)
            self.assertIn(f"guacozy_update_connection {self.visible.id}", logged)

    def test_unknown_field_is_invalid(self):
        payload = self.tool("guacozy_create_connection", {
            "protocol": "ssh", "name": "shell", "host": "10.0.0.7",
            "parent": self.folder.id, "nope": 1,
        })
        self.assertEqual(payload["error"]["code"], "INVALID_ARGS")
        self.assertEqual(payload["error"]["retry"], "never")
        self.assertFalse(ConnectionSsh.objects.filter(name="shell").exists())

    def test_delete_non_empty_folder_is_refused(self):
        payload = self.tool("guacozy_delete_folder", {"folder_id": self.folder.id})
        self.assertEqual(payload["error"]["code"], "NOT_ALLOWED")
        self.assertTrue(Folder.objects.filter(pk=self.folder.id).exists())

    def test_missing_parent_is_invalid(self):
        folder = self.tool("guacozy_create_folder", {"name": "orphan"})
        self.assertEqual(folder["error"]["code"], "INVALID_ARGS")
        self.assertEqual(folder["error"]["retry"], "never")
        self.assertFalse(Folder.objects.filter(name="orphan").exists())
        connection = self.tool("guacozy_create_connection", {
            "protocol": "rdp", "name": "noparent", "host": "10.0.0.8",
        })
        self.assertEqual(connection["error"]["code"], "INVALID_ARGS")
        self.assertEqual(connection["error"]["retry"], "never")

    def test_ssh_secrets_are_stored_but_omitted(self):
        with self.assertLogs("backend.agent.management", level="INFO") as captured:
            payload = self.tool("guacozy_create_connection", {
                "protocol": "ssh",
                "name": "shellbox",
                "host": "10.0.0.7",
                "parent": self.folder.id,
                "private_key": "BEGIN KEY",
                "passphrase": "phrase",
                "password": "ssh-secret",
                "command": "uptime",
                "font_size": 12,
            })
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["data"]["command"], "uptime")
            self.assertEqual(payload["data"]["font_size"], 12)
            self.assertEqual(payload["data"]["port"], 22)
            self.assertTrue(payload["data"]["has_private_key"])
            self.assertTrue(payload["data"]["has_password"])
            self.assertNotIn("private_key", payload["data"])
            self.assertNotIn("passphrase", payload["data"])
            self.assertNotIn("password", payload["data"])
            self.assertEqual(
                set(payload["data"]),
                {
                    "id", "name", "protocol", "parent", "host", "port", "username",
                    "domain", "passthrough_credentials", "has_password",
                    "has_private_key", "guacdserver", "host_key",
                    "server_alive_interval", "font_size", "command",
                },
            )
            logged = "\n".join(captured.output)
            for secret in ("BEGIN KEY", "phrase", "ssh-secret"):
                self.assertNotIn(secret, logged)
        stored = ConnectionSsh.objects.get(name="shellbox")
        self.assertEqual(stored.private_key, "BEGIN KEY")
        self.assertEqual(stored.passphrase, "phrase")
        self.assertEqual(stored.password, "ssh-secret")
        self.assertEqual(stored.command, "uptime")

    def test_no_staff_bypass_for_folders_or_connections(self):
        hidden_folder = Folder.objects.create(name="Hidden")
        hidden = ConnectionRdp.objects.create(
            name="HIDDEN",
            host="10.9.9.9",
            parent=hidden_folder,
            password="other-secret",
        )
        folders = self.tool("guacozy_list_folders", {})
        self.assertTrue(folders["ok"], folders)
        self.assertEqual(
            [row["name"] for row in folders["data"]["folders"]],
            ["Lab"],
        )
        self.assertEqual(folders["data"]["folders"][0]["parent"], None)
        listed = self.tool("guacozy_list_connections", {})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(
            [row["name"] for row in listed["data"]["connections"]],
            ["VISIBLE"],
        )
        self.assertNotIn("password", listed["data"]["connections"][0])
        self.assertNotIn("other-secret", json.dumps(listed))
        missing = self.tool("guacozy_get_connection", {"connection_id": str(hidden.id)})
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")
        denied = self.tool("guacozy_create_connection", {
            "protocol": "rdp", "name": "LEAK", "host": "10.9.9.8", "parent": hidden_folder.id,
        })
        self.assertEqual(denied["error"]["code"], "NOT_ALLOWED")
        self.assertEqual(denied["error"]["retry"], "never")
        self.assertFalse(ConnectionRdp.objects.filter(name="LEAK").exists())

    def test_folder_checks_match_the_viewset(self):
        root = Folder.objects.create(name="Root")
        shared = Folder.objects.create(name="Shared", parent=root)
        FolderPermission.objects.create(folder=shared, user=self.member)
        listed = self.tool_as(self.member, "guacozy_list_folders", {})
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(
            {row["name"] for row in listed["data"]["folders"]},
            {"Lab", "Root", "Shared"},
        )
        under_root = self.tool_as(self.member, "guacozy_create_folder", {
            "name": "UnderRoot", "parent": root.id,
        })
        self.assertEqual(under_root["error"]["code"], "NOT_ALLOWED")
        created = self.tool_as(self.member, "guacozy_create_folder", {
            "name": "UnderShared", "parent": shared.id,
        })
        self.assertTrue(created["ok"], created)
        self.assertEqual(created["data"]["parent"], shared.id)
        self.assertEqual(created["data"]["name"], "UnderShared")
        rename_root = self.tool_as(self.member, "guacozy_update_folder", {
            "folder_id": root.id, "name": "Renamed",
        })
        self.assertEqual(rename_root["error"]["code"], "NOT_ALLOWED")
        self.assertEqual(Folder.objects.get(pk=root.id).name, "Root")
        renamed = self.tool("guacozy_update_folder", {
            "folder_id": self.folder.id, "name": "Lab2",
        })
        self.assertTrue(renamed["ok"], renamed)
        self.assertEqual(renamed["data"]["name"], "Lab2")
        self.assertEqual(renamed["data"]["id"], self.folder.id)
        other = Folder.objects.create(name="Other")
        moved = self.tool("guacozy_update_folder", {
            "folder_id": self.folder.id, "parent": other.id,
        })
        self.assertEqual(moved["error"]["code"], "NOT_ALLOWED")
        self.assertIsNone(Folder.objects.get(pk=self.folder.id).parent_id)
        removed = self.tool_as(self.member, "guacozy_delete_folder", {
            "folder_id": created["data"]["id"],
        })
        self.assertTrue(removed["ok"], removed)
        self.assertFalse(Folder.objects.filter(name="UnderShared").exists())

    def test_member_cannot_update_or_delete_connection(self):
        denied = self.tool_as(self.member, "guacozy_update_connection", {
            "connection_id": str(self.visible.id), "name": "hacked",
        })
        self.assertEqual(denied["error"]["code"], "NOT_ALLOWED")
        self.assertEqual(denied["error"]["retry"], "never")
        self.assertEqual(ConnectionRdp.objects.get(pk=self.visible.id).name, "VISIBLE")
        visible = self.tool_as(self.member, "guacozy_get_connection", {
            "connection_id": str(self.visible.id),
        })
        self.assertTrue(visible["ok"], visible)
        self.assertNotIn("password", visible["data"])
        self.assertTrue(visible["data"]["has_password"])
        deleted = self.tool_as(self.member, "guacozy_delete_connection", {
            "connection_id": str(self.visible.id),
        })
        self.assertEqual(deleted["error"]["code"], "NOT_ALLOWED")
        self.assertTrue(ConnectionRdp.objects.filter(pk=self.visible.id).exists())
        hidden = self.tool_as(self.outsider, "guacozy_delete_connection", {
            "connection_id": str(self.visible.id),
        })
        self.assertEqual(hidden["error"]["code"], "NOT_FOUND")
        self.assertTrue(ConnectionRdp.objects.filter(pk=self.visible.id).exists())
        removed = self.tool("guacozy_delete_connection", {"connection_id": str(self.visible.id)})
        self.assertTrue(removed["ok"], removed)
        self.assertFalse(ConnectionRdp.objects.filter(pk=self.visible.id).exists())

    def test_bad_protocol_and_cross_protocol_field_are_invalid(self):
        protocol = self.tool("guacozy_create_connection", {
            "protocol": "telnet", "name": "old", "host": "10.0.0.3", "parent": self.folder.id,
        })
        self.assertEqual(protocol["error"]["code"], "INVALID_ARGS")
        self.assertEqual(protocol["error"]["retry"], "never")
        crossed = self.tool("guacozy_create_connection", {
            "protocol": "rdp", "name": "badfield", "host": "10.0.0.9",
            "parent": self.folder.id, "command": "whoami",
        })
        self.assertEqual(crossed["error"]["code"], "INVALID_ARGS")
        self.assertEqual(crossed["error"]["retry"], "never")
        self.assertFalse(ConnectionRdp.objects.filter(name="badfield").exists())

    def test_passphrase_on_rdp_or_vnc_is_invalid(self):
        for protocol, name in (("rdp", "rdp-phrase"), ("vnc", "vnc-phrase")):
            with self.subTest(protocol=protocol):
                payload = self.tool("guacozy_create_connection", {
                    "protocol": protocol,
                    "name": name,
                    "host": "10.0.0.12",
                    "parent": self.folder.id,
                    "passphrase": "phrase",
                })
                self.assertEqual(payload["error"]["code"], "INVALID_ARGS")
                self.assertEqual(payload["error"]["retry"], "never")
                self.assertFalse(ConnectionRdp.objects.filter(name=name).exists())
                self.assertFalse(ConnectionVnc.objects.filter(name=name).exists())

    def test_vnc_payload_lists_child_fields(self):
        payload = self.tool("guacozy_create_connection", {
            "protocol": "vnc",
            "name": "screen-ok",
            "host": "10.0.0.6",
            "parent": self.folder.id,
            "read_only": True,
            "autoretry": 3,
        })
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"]["protocol"], "vnc")
        self.assertEqual(payload["data"]["port"], 5901)
        self.assertEqual(payload["data"]["read_only"], True)
        self.assertEqual(payload["data"]["autoretry"], 3)
        self.assertFalse(payload["data"]["has_password"])
        self.assertFalse(payload["data"]["has_private_key"])
        self.assertNotIn("password", payload["data"])
        self.assertNotIn("private_key", payload["data"])
        self.assertEqual(
            set(payload["data"]),
            {
                "id", "name", "protocol", "parent", "host", "port", "username",
                "domain", "passthrough_credentials", "has_password",
                "has_private_key", "guacdserver", "autoretry", "color_depth",
                "swap_red_blue", "cursor_remote", "read_only", "repeater_dest_host",
                "repeater_dest_port", "reverse_connect", "listen_timeout",
                "clipboard_encoding",
            },
        )
