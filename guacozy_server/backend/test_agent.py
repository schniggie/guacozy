import base64
import io
import json
import logging
import socket
import struct
import threading
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone
from guacamole.client import GuacamoleClient
from guacamole.instruction import GuacamoleInstruction
from PIL import Image

from backend.agent.frame import Frame
from backend.agent.tokens import authenticate_bearer, create_agent_token, hash_token
from backend.models import (
    AgentToken,
    ConnectionRdp,
    ConnectionSsh,
    ConnectionVnc,
    Folder,
    FolderPermission,
    GuacdServer,
    Ticket,
    TicketLog,
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
                "guacozy_set_folder_access",
                "guacozy_list_tickets",
                "guacozy_create_ticket",
                "guacozy_delete_ticket",
                "guacozy_open_desktop",
                "guacozy_snapshot",
                "guacozy_click",
                "guacozy_type",
                "guacozy_press",
                "guacozy_scroll",
                "guacozy_clipboard_get",
                "guacozy_clipboard_set",
                "guacozy_close_desktop",
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


class AgentTicketTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="ticket-staff",
            password="secret",
            is_staff=True,
        )
        self.member = User.objects.create_user(
            username="ticket-member",
            password="secret",
            is_staff=False,
        )
        self.outsider = User.objects.create_user(
            username="ticket-outsider",
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

    def test_staff_can_grant_folder_access(self):
        FolderPermission.objects.filter(folder=self.folder, user=self.member).delete()
        self.assertFalse(self.folder.users.filter(pk=self.member.id).exists())
        payload = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "grant", "user_id": self.member.id,
        })
        self.assertTrue(payload["ok"])
        self.assertTrue(self.folder.users.filter(pk=self.member.id).exists())
        self.assertEqual(
            FolderPermission.objects.filter(folder=self.folder, user=self.member).count(),
            1,
        )
        again = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "grant", "user_id": self.member.id,
        })
        self.assertTrue(again["ok"], again)
        self.assertEqual(
            FolderPermission.objects.filter(folder=self.folder, user=self.member).count(),
            1,
        )

    def test_non_staff_set_folder_access_is_not_allowed(self):
        before = set(FolderPermission.objects.filter(folder=self.folder).values_list("pk", flat=True))
        payload = self.tool_as(self.member, "guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "grant", "user_id": self.outsider.id,
        })
        self.assertEqual(payload["error"]["code"], "NOT_ALLOWED")
        self.assertEqual(payload["error"]["retry"], "never")
        self.assertFalse(self.folder.users.filter(pk=self.outsider.id).exists())
        hidden = Folder.objects.create(name="Hidden")
        missing = self.tool_as(self.member, "guacozy_set_folder_access", {
            "folder_id": hidden.id, "action": "revoke", "user_id": self.staff.id,
        })
        self.assertEqual(missing["error"]["code"], "NOT_ALLOWED")
        self.assertEqual(missing["error"]["retry"], "never")
        self.assertEqual(
            set(FolderPermission.objects.filter(folder=self.folder).values_list("pk", flat=True)),
            before,
        )

    def test_folder_access_needs_exactly_one_principal(self):
        both = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id,
            "action": "grant",
            "user_id": self.outsider.id,
            "group_id": 1,
        })
        self.assertEqual(both["error"]["code"], "INVALID_ARGS")
        self.assertEqual(both["error"]["retry"], "never")
        self.assertFalse(self.folder.users.filter(pk=self.outsider.id).exists())
        neither = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "revoke",
        })
        self.assertEqual(neither["error"]["code"], "INVALID_ARGS")
        self.assertEqual(neither["error"]["retry"], "never")
        self.assertTrue(self.folder.users.filter(pk=self.member.id).exists())
        bad_action = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "allow", "user_id": self.outsider.id,
        })
        self.assertEqual(bad_action["error"]["code"], "INVALID_ARGS")
        self.assertEqual(bad_action["error"]["retry"], "never")

    def test_staff_can_grant_any_folder_and_revoke(self):
        hidden = Folder.objects.create(name="Hidden")
        granted = self.tool("guacozy_set_folder_access", {
            "folder_id": hidden.id, "action": "grant", "user_id": self.outsider.id,
        })
        self.assertTrue(granted["ok"], granted)
        self.assertTrue(hidden.users.filter(pk=self.outsider.id).exists())
        self.assertEqual(
            FolderPermission.objects.filter(folder=hidden, user=self.outsider).count(),
            1,
        )
        revoked = self.tool("guacozy_set_folder_access", {
            "folder_id": hidden.id, "action": "revoke", "user_id": self.outsider.id,
        })
        self.assertTrue(revoked["ok"], revoked)
        self.assertFalse(hidden.users.filter(pk=self.outsider.id).exists())
        missing = self.tool("guacozy_set_folder_access", {
            "folder_id": 999999, "action": "grant", "user_id": self.member.id,
        })
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")
        self.assertEqual(missing["error"]["retry"], "never")
        unknown_user = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "grant", "user_id": 999999,
        })
        self.assertEqual(unknown_user["error"]["code"], "NOT_FOUND")

    def test_staff_can_grant_and_revoke_a_group(self):
        group = Group.objects.create(name="ops")
        other = Group.objects.create(name="other")
        FolderPermission.objects.create(folder=self.folder, group=other)
        granted = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "grant", "group_id": group.id,
        })
        self.assertTrue(granted["ok"], granted)
        self.assertTrue(self.folder.groups.filter(pk=group.id).exists())
        self.assertEqual(
            FolderPermission.objects.filter(folder=self.folder, group=group).count(),
            1,
        )
        self.assertTrue(self.folder.users.filter(pk=self.member.id).exists())
        revoked = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "revoke", "group_id": group.id,
        })
        self.assertTrue(revoked["ok"], revoked)
        self.assertFalse(self.folder.groups.filter(pk=group.id).exists())
        self.assertTrue(self.folder.groups.filter(pk=other.id).exists())
        self.assertTrue(self.folder.users.filter(pk=self.member.id).exists())
        unknown = self.tool("guacozy_set_folder_access", {
            "folder_id": self.folder.id, "action": "grant", "group_id": 999999,
        })
        self.assertEqual(unknown["error"]["code"], "NOT_FOUND")

    def test_member_ticket_is_theirs(self):
        payload = self.tool_as(self.member, "guacozy_create_ticket", {"connection_id": str(self.visible.id)})
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["data"]["connection_id"], str(self.visible.id))
        ticket = Ticket.objects.get(pk=payload["data"]["id"])
        self.assertEqual(ticket.user, self.member)
        self.assertEqual(ticket.author, self.member)
        self.assertTrue(ticket.control)
        self.assertIsNone(ticket.parent_id)
        self.assertEqual(ticket.validityperiod, timedelta(seconds=86400))
        self.assertEqual(
            set(payload["data"]),
            {"id", "connection_id", "control", "parent", "validto"},
        )
        self.assertEqual(payload["data"]["id"], str(ticket.id))
        self.assertEqual(payload["data"]["control"], True)
        self.assertIsNone(payload["data"]["parent"])
        self.assertEqual(
            payload["data"]["validto"],
            timezone.localtime(ticket.created + ticket.validityperiod).isoformat(),
        )
        datetime.fromisoformat(payload["data"]["validto"])
        second = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
        })
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(second["data"]["id"], payload["data"]["id"])

    def test_list_tickets_does_not_delete_other_expired_tickets(self):
        created = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
        })
        self.assertTrue(created["ok"], created)
        parent = Ticket.objects.create(
            connection=self.visible,
            user=self.member,
            author=self.member,
        )
        child = Ticket.objects.create(
            connection=self.visible,
            user=self.outsider,
            author=self.member,
            parent=parent,
            control=False,
        )
        held_expired = Ticket.objects.create(
            connection=self.visible,
            user=self.member,
            author=self.staff,
            validityperiod=timedelta(seconds=60),
        )
        other_folder = Folder.objects.create(name="Elsewhere")
        other_conn = ConnectionRdp.objects.create(
            name="OTHER",
            host="10.1.1.1",
            parent=other_folder,
        )
        other_expired = Ticket.objects.create(
            connection=other_conn,
            user=self.outsider,
            author=self.outsider,
            validityperiod=timedelta(seconds=60),
        )
        expired_at = timezone.now() - timedelta(days=30)
        Ticket.objects.filter(pk__in=[held_expired.pk, other_expired.pk]).update(created=expired_at)

        outsider_list = self.tool_as(self.outsider, "guacozy_list_tickets", {})
        self.assertTrue(outsider_list["ok"], outsider_list)
        self.assertTrue(Ticket.objects.filter(pk=held_expired.pk).exists())
        self.assertTrue(Ticket.objects.filter(pk=other_expired.pk).exists())
        outsider_ids = {row["id"] for row in outsider_list["data"]["tickets"]}
        self.assertIn(str(child.id), outsider_ids)
        self.assertIn(str(other_expired.id), outsider_ids)
        self.assertNotIn(str(held_expired.id), outsider_ids)
        self.assertNotIn(created["data"]["id"], outsider_ids)

        listed = self.tool_as(self.member, "guacozy_list_tickets", {})
        self.assertTrue(listed["ok"], listed)
        rows = {row["id"]: row for row in listed["data"]["tickets"]}
        self.assertIn(created["data"]["id"], rows)
        self.assertIn(str(parent.id), rows)
        self.assertIn(str(child.id), rows)
        self.assertIn(str(held_expired.id), rows)
        self.assertNotIn(str(other_expired.id), rows)
        self.assertIsNone(rows[str(parent.id)]["parent"])
        self.assertEqual(rows[str(child.id)]["parent"], str(parent.id))
        self.assertEqual(rows[str(child.id)]["control"], False)
        self.assertEqual(rows[str(child.id)]["connection_id"], str(self.visible.id))
        self.assertEqual(
            set(rows[created["data"]["id"]]),
            {"id", "connection_id", "control", "parent", "validto"},
        )
        self.assertTrue(Ticket.objects.filter(pk=other_expired.pk).exists())
        self.assertTrue(Ticket.objects.filter(pk=held_expired.pk).exists())

        staff_ids = {row["id"] for row in self.tool("guacozy_list_tickets", {})["data"]["tickets"]}
        self.assertIn(str(held_expired.id), staff_ids)
        self.assertNotIn(created["data"]["id"], staff_ids)
        self.assertNotIn(str(other_expired.id), staff_ids)

    def test_delete_ticket_only_author_or_holder(self):
        created = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
        })
        self.assertTrue(created["ok"], created)
        ticket_id = created["data"]["id"]
        denied = self.tool_as(self.outsider, "guacozy_delete_ticket", {"ticket_id": ticket_id})
        self.assertEqual(denied["error"]["code"], "NOT_FOUND")
        self.assertEqual(denied["error"]["retry"], "never")
        self.assertTrue(Ticket.objects.filter(pk=ticket_id).exists())
        removed = self.tool_as(self.member, "guacozy_delete_ticket", {"ticket_id": ticket_id})
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(removed["data"]["id"], ticket_id)
        self.assertEqual(removed["data"]["connection_id"], str(self.visible.id))
        self.assertEqual(
            set(removed["data"]),
            {"id", "connection_id", "control", "parent", "validto"},
        )
        self.assertFalse(Ticket.objects.filter(pk=ticket_id).exists())

        authored = Ticket.objects.create(
            connection=self.visible,
            user=self.outsider,
            author=self.member,
        )
        by_author = self.tool_as(self.member, "guacozy_delete_ticket", {"ticket_id": str(authored.id)})
        self.assertTrue(by_author["ok"], by_author)
        self.assertFalse(Ticket.objects.filter(pk=authored.id).exists())

        held = Ticket.objects.create(
            connection=self.visible,
            user=self.member,
            author=self.staff,
            validityperiod=timedelta(seconds=60),
        )
        Ticket.objects.filter(pk=held.pk).update(created=timezone.now() - timedelta(days=30))
        by_holder = self.tool_as(self.member, "guacozy_delete_ticket", {"ticket_id": str(held.id)})
        self.assertTrue(by_holder["ok"], by_holder)
        self.assertFalse(Ticket.objects.filter(pk=held.id).exists())
        malformed = self.tool("guacozy_delete_ticket", {"ticket_id": "not-a-uuid"})
        self.assertEqual(malformed["error"]["code"], "INVALID_ARGS")
        self.assertEqual(malformed["error"]["retry"], "never")

    def test_hidden_connection_ticket_is_not_found(self):
        visible = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
        })
        self.assertTrue(visible["ok"], visible)
        payload = self.tool_as(self.outsider, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
        })
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")
        self.assertEqual(payload["error"]["retry"], "never")
        self.assertFalse(Ticket.objects.filter(user=self.outsider).exists())
        hidden_folder = Folder.objects.create(name="Hidden")
        hidden = ConnectionRdp.objects.create(
            name="HIDDEN",
            host="10.9.9.9",
            parent=hidden_folder,
        )
        staff_hidden = self.tool("guacozy_create_ticket", {"connection_id": str(hidden.id)})
        self.assertEqual(staff_hidden["error"]["code"], "NOT_FOUND")
        self.assertFalse(Ticket.objects.filter(connection=hidden).exists())

    def test_create_ticket_validity_seconds(self):
        bad = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
            "validity_seconds": True,
        })
        self.assertEqual(bad["error"]["code"], "INVALID_ARGS")
        self.assertEqual(bad["error"]["retry"], "never")
        negative = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
            "validity_seconds": -1,
        })
        self.assertEqual(negative["error"]["code"], "INVALID_ARGS")
        self.assertFalse(Ticket.objects.filter(user=self.member).exists())
        payload = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
            "validity_seconds": 120,
        })
        self.assertTrue(payload["ok"], payload)
        ticket = Ticket.objects.get(pk=payload["data"]["id"])
        self.assertEqual(ticket.validityperiod, timedelta(seconds=120))
        self.assertEqual(ticket.user, self.member)
        self.assertEqual(ticket.author, self.member)
        self.assertTrue(ticket.control)
        self.assertIsNone(ticket.parent_id)
        self.assertEqual(
            payload["data"]["validto"],
            timezone.localtime(ticket.created + ticket.validityperiod).isoformat(),
        )

    def test_unrepresentable_lifetime_creates_nothing(self):
        rejected = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
            "validity_seconds": 10**12,
        })
        self.assertEqual(rejected["error"]["code"], "INVALID_ARGS")
        self.assertEqual(rejected["error"]["retry"], "never")
        self.assertFalse(Ticket.objects.filter(connection=self.visible).exists())

        bad = Ticket.objects.create(
            connection=self.visible,
            user=self.member,
            author=self.member,
            validityperiod=timedelta(seconds=10**12),
        )
        good = self.tool_as(self.member, "guacozy_create_ticket", {
            "connection_id": str(self.visible.id),
        })
        self.assertTrue(good["ok"], good)
        listed = self.tool_as(self.member, "guacozy_list_tickets", {})
        self.assertTrue(listed["ok"], listed)
        rows = {row["id"]: row for row in listed["data"]["tickets"]}
        self.assertIn(str(bad.id), rows)
        self.assertIn(good["data"]["id"], rows)
        self.assertIsNone(rows[str(bad.id)]["validto"])
        removed = self.tool_as(self.member, "guacozy_delete_ticket", {"ticket_id": str(bad.id)})
        self.assertTrue(removed["ok"], removed)
        self.assertIsNone(removed["data"]["validto"])
        self.assertFalse(Ticket.objects.filter(pk=bad.id).exists())


def _red_png_b64():
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 0, 0)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


RED_PNG_B64 = _red_png_b64()


class FrameTests(SimpleTestCase):
    def test_png_blob_is_pasted_at_xy(self):
        frame = Frame()
        frame.apply(GuacamoleInstruction("size", "0", "2", "2"))
        # 1x1 red PNG, base64, declared at x=1, y=0
        frame.apply(GuacamoleInstruction("img", "3", "0", "0", "image/png", "1", "0"))
        frame.apply(GuacamoleInstruction("blob", "3", RED_PNG_B64))
        frame.apply(GuacamoleInstruction("end", "3"))
        image = Image.open(io.BytesIO(frame.png_bytes()))
        self.assertEqual(image.getpixel((1, 0))[:3], (255, 0, 0))
        self.assertEqual(image.size, (2, 2))

    def test_copy_moves_the_pixel_and_cursor_is_not_drawn(self):
        frame = Frame()
        frame.apply(GuacamoleInstruction("size", "0", "2", "2"))
        frame.apply(GuacamoleInstruction("img", "3", "0", "0", "image/png", "1", "0"))
        frame.apply(GuacamoleInstruction("blob", "3", RED_PNG_B64))
        frame.apply(GuacamoleInstruction("end", "3"))
        frame.apply(GuacamoleInstruction("copy", "0", "1", "0", "1", "1", "0", "0", "0", "1"))
        frame.apply(GuacamoleInstruction("cursor", "4", "5", "0", "0", "0", "0", "0"))
        image = Image.open(io.BytesIO(frame.png_bytes()))
        self.assertEqual(image.getpixel((0, 1))[:3], (255, 0, 0))
        self.assertEqual(frame.cursor, (4, 5))

    def test_cfill_fills_the_rect_on_that_layer(self):
        frame = Frame()
        frame.apply(GuacamoleInstruction("size", "0", "2", "2"))
        before = frame.png_bytes()
        frame.apply(GuacamoleInstruction("cfill", "1", "0", "0", "0", "255", "255"))
        self.assertEqual(frame.png_bytes(), before)
        frame.apply(GuacamoleInstruction("rect", "0", "0", "1", "1", "1"))
        frame.apply(GuacamoleInstruction("cfill", "1", "0", "0", "0", "255", "255"))
        image = Image.open(io.BytesIO(frame.png_bytes()))
        self.assertEqual(image.getpixel((0, 1))[:3], (0, 0, 255))

    def test_partial_alpha_png_is_source_over(self):
        def png(color):
            buffer = io.BytesIO()
            Image.new("RGBA", (1, 1), color).save(buffer, format="PNG")
            return base64.b64encode(buffer.getvalue()).decode("ascii")

        frame = Frame()
        frame.apply(GuacamoleInstruction("size", "0", "3", "1"))
        frame.apply(GuacamoleInstruction("img", "1", "0", "0", "image/png", "0", "0"))
        frame.apply(GuacamoleInstruction("blob", "1", png((255, 0, 0, 128))))
        frame.apply(GuacamoleInstruction("end", "1"))
        frame.apply(GuacamoleInstruction("rect", "0", "1", "0", "1", "1"))
        frame.apply(GuacamoleInstruction("cfill", "1", "0", "0", "255", "0", "255"))
        frame.apply(GuacamoleInstruction("img", "2", "0", "0", "image/png", "1", "0"))
        frame.apply(GuacamoleInstruction("blob", "2", png((255, 0, 0, 128))))
        frame.apply(GuacamoleInstruction("end", "2"))
        frame.apply(GuacamoleInstruction("rect", "0", "2", "0", "1", "1"))
        frame.apply(GuacamoleInstruction("cfill", "1", "0", "0", "255", "0", "255"))
        frame.apply(GuacamoleInstruction("img", "3", "0", "0", "image/png", "2", "0"))
        frame.apply(GuacamoleInstruction("blob", "3", png((0, 0, 0, 0))))
        frame.apply(GuacamoleInstruction("end", "3"))
        image = Image.open(io.BytesIO(frame.png_bytes()))
        self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 128))
        self.assertEqual(image.getpixel((1, 0)), (128, 127, 0, 255))
        self.assertEqual(image.getpixel((2, 0)), (0, 255, 0, 255))


def _closed_local_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _FakeGuacd:
    """Local guacd. Reads until connect, so a join is not required to send size."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.sent = []
        self.server_args = ()
        self.mouse_seen = threading.Event()
        self.agent = None
        self.agent_id = None
        self.reject_next_join = False
        self._stop = False
        self._lock = threading.Lock()
        self._threads = []
        self._clients = []
        self._ready = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        if not self._ready.wait(2):
            raise RuntimeError("fake guacd did not start")

    def sent_copy(self):
        with self._lock:
            return list(self.sent)

    @property
    def opcodes(self):
        return [opcode for opcode, _args in self.sent_copy()]

    def send_to_agent(self, instructions):
        payload = "".join(item.encode() for item in instructions).encode()
        with self._lock:
            sock = self.agent
        if sock is None:
            raise RuntimeError("agent socket is not connected")
        sock.sendall(payload)

    def stop(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        with self._lock:
            clients = [thread_conn for thread_conn in self._clients]
        for conn in clients:
            self._reset(conn)
        self.thread.join(timeout=2)
        for worker in list(self._threads):
            worker.join(timeout=2)

    def _reset(self, conn):
        # RST, so a later send on the agent socket fails instead of succeeding after FIN.
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _accept(self):
        self.sock.settimeout(0.2)
        self._ready.set()
        while not self._stop:
            try:
                conn, _addr = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._lock:
                self._clients.append(conn)
            worker = threading.Thread(target=self._client, args=(conn,), daemon=True)
            self._threads.append(worker)
            worker.start()

    def _record(self, instruction):
        with self._lock:
            self.sent.append((instruction.opcode, list(instruction.args)))
        if instruction.opcode == "mouse":
            self.mouse_seen.set()

    def _pull(self, conn, buf, timeout):
        conn.settimeout(timeout)
        while b";" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None, buf
            buf += chunk
        raw, buf = buf.split(b";", 1)
        return GuacamoleInstruction.load(raw.decode() + ";"), buf

    def _client(self, conn):
        buf = b""
        try:
            select, buf = self._pull(conn, buf, 5)
            if select is None:
                return
            self._record(select)
            reject = False
            if select.args and str(select.args[0]).startswith("$"):
                with self._lock:
                    if self.reject_next_join:
                        self.reject_next_join = False
                        reject = True
            if reject:
                # Drop the join before args/ready. The next client is a new session.
                self._reset(conn)
                return
            args = tuple(self.server_args)
            conn.sendall(GuacamoleInstruction("args", *args).encode().encode())
            while True:
                instruction, buf = self._pull(conn, buf, 5)
                if instruction is None:
                    return
                self._record(instruction)
                if instruction.opcode == "connect":
                    break
            ready_id = "$" + str(uuid.uuid4())
            with self._lock:
                if self.agent is None:
                    self.agent = conn
                    self.agent_id = ready_id
            conn.sendall(GuacamoleInstruction("ready", ready_id).encode().encode())
            while not self._stop:
                try:
                    instruction, buf = self._pull(conn, buf, 0.2)
                except TimeoutError:
                    continue
                if instruction is None:
                    return
                self._record(instruction)
        except OSError:
            return


class _OtherJoin:
    """A second guacd client blocked in read. Closing the agent must not close it."""

    def __init__(self, port, connection_id):
        self.alive = False
        self._stop = False
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.sendall(GuacamoleInstruction("select", connection_id).encode().encode())
        self._read_one()
        payload = "".join(
            GuacamoleInstruction(opcode, *args).encode()
            for opcode, args in (
                ("audio", ()),
                ("video", ()),
                ("image", ("image/png",)),
                ("connect", ()),
            )
        )
        self.sock.sendall(payload.encode())
        self._read_one()
        self.alive = True
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _read_one(self):
        self.sock.settimeout(5)
        data = b""
        while b";" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("closed during handshake")
            data += chunk

    def _loop(self):
        try:
            while not self._stop:
                try:
                    chunk = self.sock.recv(4096)
                except TimeoutError:
                    continue
                if not chunk:
                    self.alive = False
                    return
        except OSError:
            self.alive = False

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(timeout=2)


class AgentDesktopTests(TestCase):
    def setUp(self):
        super().setUp()
        self.other_join = None
        self.session_id = None
        original_init = GuacamoleClient.__init__

        def fast_timeout(client, host, port, timeout=20, debug=False, logger=None):
            # Same 0.4s budget as the tunnel test. A shared receive thread stays
            # blocked after relax_read_timeout and the click below misses the 2s wait.
            original_init(client, host, port, timeout=0.4, debug=debug, logger=logger)

        self._timeout_patch = patch.object(GuacamoleClient, "__init__", fast_timeout)
        self._timeout_patch.start()
        self.addCleanup(self._timeout_patch.stop)
        self.fake = _FakeGuacd()
        self.staff = User.objects.create_user(
            username="desktop-staff",
            password="secret",
            is_staff=True,
        )
        self.member = User.objects.create_user(
            username="desktop-member",
            password="secret",
            is_staff=False,
        )
        self.outsider = User.objects.create_user(
            username="desktop-outsider",
            password="secret",
            is_staff=False,
        )
        self.folder = Folder.objects.create(name="Desktop lab")
        FolderPermission.objects.create(folder=self.folder, user=self.member)
        FolderPermission.objects.create(folder=self.folder, user=self.staff)
        self.guacd = GuacdServer.objects.create(
            name="desktop-guacd",
            hostname="127.0.0.1",
            port=self.fake.port,
        )
        self.connection = ConnectionSsh.objects.create(
            name="DESKTOP",
            host="10.0.0.8",
            parent=self.folder,
            password="s3cret",
            guacdserver=self.guacd,
        )
        self.ticket = Ticket.objects.create(
            connection=self.connection,
            user=self.member,
            author=self.member,
        )
        _token, self.staff_secret = create_agent_token(self.staff, "staff-desktop")
        _token, self.member_secret = create_agent_token(self.member, "member-desktop")
        _token, self.outsider_secret = create_agent_token(self.outsider, "outsider-desktop")

    def tearDown(self):
        try:
            self._close_sessions()
        finally:
            try:
                if self.other_join is not None:
                    self.other_join.close()
            finally:
                self.fake.stop()
        super().tearDown()

    def _close_sessions(self):
        try:
            from backend.agent import desktop as desktop_module
        except ImportError:
            return
        sessions = getattr(desktop_module, "_SESSIONS", None)
        lock = getattr(desktop_module, "_SESSIONS_LOCK", None)
        if sessions is None or lock is None:
            return
        with lock:
            running = list(sessions.values())
            sessions.clear()
        for session in running:
            session.shutdown()

    def tool(self, name, arguments):
        return self.tool_as(self.member, name, arguments)

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

    def _open(self, **arguments):
        if not arguments:
            arguments = {"ticket_id": str(self.ticket.id)}
        payload = self.tool("guacozy_open_desktop", arguments)
        self.assertTrue(payload["ok"], payload)
        self.session_id = payload["data"]["session_id"]
        return payload

    def _wait_sent(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.fake.sent_copy()
            if predicate(snapshot):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def test_open_new_snapshot_pixels(self):
        self.assertFalse(self.member.is_staff)
        payload = self._open(connection_id=str(self.connection.id))
        self.assertFalse(payload["data"]["joined"])
        self.assertEqual(payload["data"]["width"], 1280)
        self.assertEqual(payload["data"]["height"], 720)
        self.assertEqual(
            set(payload["data"]),
            {"session_id", "ticket_id", "width", "height", "joined"},
        )
        ticket = Ticket.objects.get(pk=payload["data"]["ticket_id"])
        self.assertNotEqual(ticket.id, self.ticket.id)
        self.assertEqual(ticket.user, self.member)
        self.assertEqual(ticket.author, self.member)
        self.assertTrue(ticket.control)
        self.assertIsNone(ticket.parent_id)
        self.assertEqual(ticket.validityperiod, timedelta(days=1))
        self.assertEqual(self.fake.agent_id, "$" + str(ticket.sessionid))
        log = TicketLog.objects.get(ticketid=ticket.id)
        self.assertEqual(log.action, "connect")
        self.assertIsNone(log.ip)
        sent = self.fake.sent_copy()
        self.assertEqual([opcode for opcode, _args in sent][0], "select")
        self.assertEqual(
            [args for opcode, args in sent if opcode == "select"],
            [["ssh"]],
        )
        self.assertEqual(
            [opcode for opcode, _args in sent if opcode != "select"],
            ["size", "audio", "video", "image", "connect"],
        )
        self.assertEqual(
            [args for opcode, args in sent if opcode == "size"],
            [["1280", "720", "96"]],
        )
        # 1x1 red PNG at x=1, y=0, then sync timestamp 1. Size must precede the paste.
        self.fake.send_to_agent([
            GuacamoleInstruction("size", "0", "2", "2"),
            GuacamoleInstruction("img", "3", "0", "0", "image/png", "1", "0"),
            GuacamoleInstruction("blob", "3", RED_PNG_B64),
            GuacamoleInstruction("end", "3"),
            GuacamoleInstruction("sync", "1"),
        ])
        # Snapshot returns the last frame immediately. Poll until the red pixel is published.
        deadline = time.monotonic() + 2
        snap = None
        while True:
            snap = self.tool("guacozy_snapshot", {"session_id": self.session_id})
            self.assertTrue(snap["ok"], snap)
            raw = snap["data"]["png_base64"]
            if raw:
                image = Image.open(io.BytesIO(base64.b64decode(raw)))
                if image.size == (2, 2) and image.getpixel((1, 0))[:3] == (255, 0, 0):
                    break
            self.assertLess(time.monotonic(), deadline, "frame was not published")
            time.sleep(0.01)
        self.assertEqual(
            snap["data"]["note"],
            "Pixels, origin top left. There are no element refs.",
        )
        self.assertEqual(
            set(snap["data"]),
            {"session_id", "width", "height", "png_base64", "cursor", "note"},
        )
        self.assertEqual(snap["data"]["session_id"], self.session_id)
        self.assertEqual(snap["data"]["width"], 2)
        self.assertEqual(snap["data"]["height"], 2)
        self.assertEqual(snap["data"]["cursor"], {"x": 0, "y": 0})
        image = Image.open(io.BytesIO(base64.b64decode(snap["data"]["png_base64"])))
        self.assertEqual(image.getpixel((1, 0))[:3], (255, 0, 0))
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertTrue(self._wait_sent(
            lambda rows: ["1"] in [args for opcode, args in rows if opcode == "sync"],
        ))
        syncs = [args for opcode, args in self.fake.sent if opcode == "sync"]
        self.assertIn(["1"], syncs)

    def test_join_does_not_send_size(self):
        existing = uuid.uuid4()
        self.ticket.sessionid = existing
        self.ticket.save()
        payload = self.tool("guacozy_open_desktop", {"ticket_id": str(self.ticket.id)})
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["data"]["joined"])
        self.assertEqual(payload["data"]["width"], 0)
        self.assertEqual(payload["data"]["height"], 0)
        self.assertEqual(payload["data"]["ticket_id"], str(self.ticket.id))
        self.assertNotIn("size", self.fake.opcodes)
        self.assertEqual(
            [opcode for opcode, _args in self.fake.sent_copy() if opcode != "select"],
            ["audio", "video", "image", "connect"],
        )
        self.assertEqual(
            [args for opcode, args in self.fake.sent_copy() if opcode == "select"],
            [["$" + str(existing)]],
        )
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.sessionid, existing)

    def test_dead_join_opens_a_new_session(self):
        dead = uuid.uuid4()
        self.ticket.sessionid = dead
        self.ticket.save(update_fields=["sessionid"])
        self.fake.reject_next_join = True
        payload = self.tool("guacozy_open_desktop", {"ticket_id": str(self.ticket.id)})
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(payload["data"]["joined"])
        self.assertEqual(payload["data"]["width"], 1280)
        self.assertEqual(payload["data"]["height"], 720)
        self.assertIn("size", self.fake.opcodes)
        self.ticket.refresh_from_db()
        self.assertNotEqual(self.ticket.sessionid, dead)
        self.assertEqual(self.fake.agent_id, "$" + str(self.ticket.sessionid))

    def test_quiet_snapshot_returns_immediately(self):
        self._open()
        started = time.monotonic()
        snap = self.tool("guacozy_snapshot", {"session_id": self.session_id})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1)
        self.assertTrue(snap["ok"], snap)
        self.assertNotEqual(snap.get("error", {}).get("code"), "STALE_SESSION")
        self.assertEqual(snap["data"]["width"], 0)
        self.assertEqual(snap["data"]["height"], 0)
        self.assertEqual(snap["data"]["png_base64"], "")
        self.assertEqual(
            snap["data"]["note"],
            "Pixels, origin top left. There are no element refs.",
        )

    def test_child_ticket_joins_the_parent_session(self):
        existing = uuid.uuid4()
        self.ticket.sessionid = existing
        self.ticket.save()
        child = Ticket.objects.create(
            connection=self.connection,
            user=self.outsider,
            author=self.member,
            parent=self.ticket,
            control=False,
        )
        payload = self.tool("guacozy_open_desktop", {"ticket_id": str(child.id)})
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["data"]["joined"])
        self.assertNotIn("size", self.fake.opcodes)
        self.assertEqual(
            [args for opcode, args in self.fake.sent_copy() if opcode == "select"],
            [["$" + str(existing)]],
        )
        child.refresh_from_db()
        self.ticket.refresh_from_db()
        self.assertIsNone(child.sessionid)
        self.assertEqual(self.ticket.sessionid, existing)

    def test_new_session_is_stored_on_the_parent_ticket(self):
        child = Ticket.objects.create(
            connection=self.connection,
            user=self.member,
            author=self.member,
            parent=self.ticket,
        )
        payload = self.tool("guacozy_open_desktop", {"ticket_id": str(child.id)})
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(payload["data"]["joined"])
        self.assertEqual(payload["data"]["width"], 1280)
        self.assertEqual(payload["data"]["height"], 720)
        self.assertIn("size", self.fake.opcodes)
        child.refresh_from_db()
        self.ticket.refresh_from_db()
        self.assertIsNone(child.sessionid)
        self.assertEqual(self.fake.agent_id, "$" + str(self.ticket.sessionid))

    def test_second_open_joins_the_stored_session(self):
        first = self._open()
        self.assertFalse(first["data"]["joined"])
        self.ticket.refresh_from_db()
        stored = self.ticket.sessionid
        before = len(self.fake.sent_copy())
        second = self.tool("guacozy_open_desktop", {"ticket_id": str(self.ticket.id)})
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["data"]["joined"])
        new_ops = [opcode for opcode, _args in self.fake.sent_copy()[before:]]
        self.assertNotIn("size", new_ops)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.sessionid, stored)

    def test_new_session_uses_connection_parameters_without_logging_them(self):
        self.fake.server_args = ("hostname", "password", "font-size")
        with self.assertLogs("backend.agent", level="DEBUG") as captured:
            self._open()
            logging.getLogger("backend.agent").info("parameters checked")
        self.assertNotIn("s3cret", "\n".join(captured.output))
        connect = [args for opcode, args in self.fake.sent_copy() if opcode == "connect"]
        self.assertEqual(connect, [["10.0.0.8", "s3cret", "8"]])

    def test_press_releases_key(self):
        self._open()
        self.tool("guacozy_press", {"session_id": self.session_id, "key": "Shift"})
        self.assertTrue(self._wait_sent(
            lambda rows: [args for opcode, args in rows if opcode == "key"] == [["65505", "1"], ["65505", "0"]],
        ))
        keys = [args for opcode, args in self.fake.sent if opcode == "key"]
        self.assertEqual(keys, [["65505", "1"], ["65505", "0"]])

    def test_type_scroll_and_buttons(self):
        self._open()
        self.assertTrue(self.tool("guacozy_type", {
            "session_id": self.session_id,
            "text": "A1",
        })["ok"])
        self.assertTrue(self._wait_sent(
            lambda rows: [args for opcode, args in rows if opcode == "key"] == [
                ["65", "1"], ["65", "0"], ["49", "1"], ["49", "0"],
            ],
        ))
        before = len(self.fake.sent_copy())
        self.assertTrue(self.tool("guacozy_click", {
            "session_id": self.session_id,
            "x": 3,
            "y": 4,
            "button": "right",
            "count": 2,
        })["ok"])
        self.assertTrue(self._wait_sent(lambda rows: len(rows) >= before + 4))
        mice = [args for opcode, args in self.fake.sent_copy()[before:] if opcode == "mouse"]
        self.assertEqual(mice, [["3", "4", "4"], ["3", "4", "0"], ["3", "4", "4"], ["3", "4", "0"]])
        before = len(self.fake.sent_copy())
        self.assertTrue(self.tool("guacozy_scroll", {
            "session_id": self.session_id,
            "x": 5,
            "y": 6,
            "delta": 6,
        })["ok"])
        self.assertTrue(self._wait_sent(lambda rows: len(rows) >= before + 10))
        ups = [args for opcode, args in self.fake.sent_copy()[before:] if opcode == "mouse"]
        self.assertEqual(ups, [["5", "6", "8"], ["5", "6", "0"]] * 5)
        before = len(self.fake.sent_copy())
        self.assertTrue(self.tool("guacozy_scroll", {
            "session_id": self.session_id,
            "x": 5,
            "y": 6,
            "delta": -2,
        })["ok"])
        self.assertTrue(self._wait_sent(lambda rows: len(rows) >= before + 4))
        downs = [args for opcode, args in self.fake.sent_copy()[before:] if opcode == "mouse"]
        self.assertEqual(downs, [["5", "6", "16"], ["5", "6", "0"]] * 2)
        bad = self.tool("guacozy_press", {"session_id": self.session_id, "key": "F1"})
        self.assertEqual(bad["error"]["code"], "INVALID_ARGS")
        self.assertEqual(bad["error"]["retry"], "never")

    def test_click_survives_a_quiet_socket(self):
        # receive is blocked in the poller; click must still arrive within 2s
        self._open()
        started = time.monotonic()
        self.tool("guacozy_click", {
            "session_id": self.session_id,
            "x": 10,
            "y": 20,
            "button": "left",
        })
        self.assertTrue(self.fake.mouse_seen.wait(2))
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(self._wait_sent(
            lambda rows: [args for opcode, args in rows if opcode == "mouse"] == [
                ["10", "20", "1"], ["10", "20", "0"],
            ],
        ))
        mice = [args for opcode, args in self.fake.sent if opcode == "mouse"]
        self.assertEqual(mice, [["10", "20", "1"], ["10", "20", "0"]])

    def test_click_failure_retry_is_unsafe(self):
        self._open()
        self.fake.stop()
        payload = self.tool("guacozy_click", {"session_id": self.session_id, "x": 1, "y": 1})
        self.assertEqual(payload["error"]["code"], "STALE_SESSION")
        self.assertEqual(payload["error"]["retry"], "unsafe")

    def test_dead_socket_retry_flags(self):
        self._open()
        self.fake.stop()
        snap = self.tool("guacozy_snapshot", {"session_id": self.session_id})
        self.assertEqual(snap["error"]["code"], "STALE_SESSION")
        self.assertEqual(snap["error"]["retry"], "safe")
        calls = (
            ("guacozy_type", {"session_id": self.session_id, "text": "a"}),
            ("guacozy_press", {"session_id": self.session_id, "key": "Enter"}),
            ("guacozy_scroll", {"session_id": self.session_id, "x": 1, "y": 1, "delta": 1}),
            ("guacozy_clipboard_get", {"session_id": self.session_id}),
            ("guacozy_clipboard_set", {"session_id": self.session_id, "text": "x"}),
        )
        for name, arguments in calls:
            payload = self.tool(name, arguments)
            self.assertEqual(payload["error"]["code"], "STALE_SESSION", name)
            self.assertEqual(payload["error"]["retry"], "unsafe", name)

    def test_clipboard_secret_is_not_logged(self):
        self._open()
        with self.assertLogs("backend.agent", level="DEBUG") as captured:
            payload = self.tool("guacozy_clipboard_set", {
                "session_id": self.session_id,
                "text": "clipboard-secret",
            })
            logging.getLogger("backend.agent").info("clipboard checked")
        self.assertTrue(payload["ok"], payload)
        logged = "\n".join(captured.output)
        self.assertNotIn("clipboard-secret", logged)
        secret = base64.b64encode(b"clipboard-secret").decode("ascii")
        self.assertNotIn(secret, logged)
        self.assertTrue(self._wait_sent(
            lambda rows: [opcode for opcode, _args in rows if opcode in {"clipboard", "blob", "end"}] == [
                "clipboard", "blob", "end",
            ],
        ))
        ops = [
            (opcode, args)
            for opcode, args in self.fake.sent
            if opcode in {"clipboard", "blob", "end"}
        ]
        self.assertEqual(len(ops), 3)
        stream = ops[0][1][0]
        self.assertEqual(ops[0][1], [stream, "text/plain"])
        self.assertEqual(ops[1], ("blob", [stream, secret]))
        self.assertEqual(ops[2], ("end", [stream]))
        got = self.tool("guacozy_clipboard_get", {"session_id": self.session_id})
        self.assertEqual(got["data"], {"text": "clipboard-secret"})
        empty = self.tool("guacozy_clipboard_get", {"session_id": self.session_id})
        self.assertEqual(empty["data"]["text"], "clipboard-secret")

    def test_other_user_session_is_not_found(self):
        self._open()
        for name, arguments in (
            ("guacozy_snapshot", {"session_id": self.session_id}),
            ("guacozy_click", {"session_id": self.session_id, "x": 1, "y": 1}),
            ("guacozy_close_desktop", {"session_id": self.session_id}),
        ):
            payload = self.tool_as(self.outsider, name, arguments)
            self.assertEqual(payload["error"]["code"], "NOT_FOUND", name)
            self.assertEqual(payload["error"]["retry"], "never", name)
        missing = self.tool("guacozy_snapshot", {"session_id": str(uuid.uuid4())})
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")
        self.assertEqual(missing["error"]["retry"], "never")
        self.assertTrue(self.tool("guacozy_press", {
            "session_id": self.session_id,
            "key": "a",
        })["ok"])

    def test_close_leaves_the_other_join_open(self):
        # second fake client joined with the same connection id and is still reading
        self._open()
        self.ticket.refresh_from_db()
        self.assertEqual(self.fake.agent_id, "$" + str(self.ticket.sessionid))
        self.other_join = _OtherJoin(self.fake.port, self.fake.agent_id)
        self.tool("guacozy_close_desktop", {"session_id": self.session_id})
        self.assertTrue(self.other_join.alive)
        time.sleep(0.2)
        self.assertTrue(self.other_join.alive)
        self.ticket.refresh_from_db()
        self.assertIsNotNone(self.ticket.sessionid)
        missing = self.tool("guacozy_snapshot", {"session_id": self.session_id})
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")
        self.assertEqual(missing["error"]["retry"], "never")

    def test_hidden_connection_is_not_found_for_staff(self):
        hidden_folder = Folder.objects.create(name="Hidden desktop")
        hidden = ConnectionSsh.objects.create(
            name="HIDDEN-DESK",
            host="10.9.9.9",
            parent=hidden_folder,
            guacdserver=self.guacd,
        )
        for user in (self.member, self.staff):
            payload = self.tool_as(user, "guacozy_open_desktop", {"connection_id": str(hidden.id)})
            self.assertEqual(payload["error"]["code"], "NOT_FOUND")
            self.assertEqual(payload["error"]["message"], "Connection not found.")
            self.assertEqual(payload["error"]["retry"], "never")
        self.assertFalse(Ticket.objects.filter(connection=hidden).exists())

    def test_open_needs_exactly_one_target(self):
        neither = self.tool("guacozy_open_desktop", {})
        both = self.tool("guacozy_open_desktop", {
            "connection_id": str(self.connection.id),
            "ticket_id": str(self.ticket.id),
        })
        extra = self.tool("guacozy_open_desktop", {
            "ticket_id": str(self.ticket.id),
            "width": 1,
        })
        for payload in (neither, both, extra):
            self.assertEqual(payload["error"]["code"], "INVALID_ARGS")
            self.assertEqual(payload["error"]["retry"], "never")
        self.assertFalse(Ticket.objects.exclude(pk=self.ticket.id).exists())
        self.assertIsNone(self.ticket.sessionid)

    def test_stranger_cannot_open_a_ticket_staff_does_not_own(self):
        shared = Ticket.objects.create(
            connection=self.connection,
            user=self.outsider,
            author=self.member,
        )
        denied = self.tool_as(self.staff, "guacozy_open_desktop", {"ticket_id": str(shared.id)})
        self.assertEqual(denied["error"]["code"], "NOT_FOUND")
        self.assertEqual(denied["error"]["retry"], "never")
        opened = self.tool_as(self.outsider, "guacozy_open_desktop", {"ticket_id": str(shared.id)})
        self.assertTrue(opened["ok"], opened)
        self.assertFalse(opened["data"]["joined"])

    def test_handshake_failure_is_remote_desktop(self):
        self.guacd.port = _closed_local_port()
        self.guacd.save(update_fields=["port"])
        payload = self.tool("guacozy_open_desktop", {"ticket_id": str(self.ticket.id)})
        self.assertEqual(payload["error"]["code"], "REMOTE_DESKTOP")
        self.assertEqual(payload["error"]["retry"], "unsafe")
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.sessionid)

    def test_failed_connection_open_deletes_the_ticket(self):
        closed = GuacdServer.objects.create(
            name="closed-desktop-guacd",
            hostname="127.0.0.1",
            port=_closed_local_port(),
        )
        other = ConnectionSsh.objects.create(
            name="DESKTOP-FAIL",
            host="10.0.0.9",
            parent=self.folder,
            password="s3cret",
            guacdserver=closed,
        )
        payload = self.tool("guacozy_open_desktop", {"connection_id": str(other.id)})
        self.assertEqual(payload["error"]["code"], "REMOTE_DESKTOP")
        self.assertEqual(payload["error"]["retry"], "unsafe")
        self.assertFalse(Ticket.objects.filter(connection=other).exists())

    def test_expired_ticket_is_not_found(self):
        self.ticket.created = timezone.now() - timedelta(days=2)
        self.ticket.validityperiod = timedelta(days=1)
        self.ticket.save(update_fields=["created", "validityperiod"])
        payload = self.tool("guacozy_open_desktop", {"ticket_id": str(self.ticket.id)})
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")
        self.assertEqual(payload["error"]["retry"], "never")
        self.assertFalse(Ticket.objects.filter(pk=self.ticket.id).exists())
