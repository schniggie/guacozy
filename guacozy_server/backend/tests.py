import asyncio
import socket
import threading
import uuid
from unittest.mock import patch

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.core.exceptions import SynchronousOnlyOperation
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import path

from backend.models import ConnectionSsh, GuacdServer, StaticCredentials, Ticket
from guacamole.client import GuacamoleClient
from guacozy_server.guacdproxy.consumers import GuacamoleConsumer
from users.models import User


def _consumer_class(callback):
    """Return the consumer class a Channels route callback points at."""
    if callback is None:
        return None
    if callback is GuacamoleConsumer:
        return callback
    for attr in ("consumer_class", "consumer"):
        value = getattr(callback, attr, None)
        if value is not None:
            return value
    return None


def _walk(node, seen):
    if node is None or isinstance(node, (str, bytes, int, float, bool)):
        return
    marker = id(node)
    if marker in seen:
        return
    seen.add(marker)
    yield node
    mapping = getattr(node, "application_mapping", None)
    if isinstance(mapping, dict):
        for child in mapping.values():
            yield from _walk(child, seen)
    routes = getattr(node, "routes", None)
    if routes:
        for route in routes:
            yield from _walk(route, seen)
    for attr in ("inner", "application", "app", "callback"):
        child = getattr(node, attr, None)
        if child is not None and child is not node:
            yield from _walk(child, seen)


def _ticket_tunnel_consumers(application):
    found = []
    for node in _walk(application, set()):
        pattern = getattr(node, "pattern", None)
        if pattern is None:
            continue
        if "tunnelws/ticket/" not in str(pattern):
            continue
        found.append(_consumer_class(getattr(node, "callback", None)))
    return found


class LoginAndAsgiRouteTests(TestCase):
    def test_api_client_can_read_the_csrf_cookie(self):
        self.assertFalse(settings.CSRF_COOKIE_HTTPONLY)
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)
        self.assertFalse(settings.CSRF_COOKIE_SECURE)
        self.assertFalse(settings.SESSION_COOKIE_SECURE)

    def test_login_page_renders_password_form(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('id="login-container"', body)
        self.assertRegex(body, r"""type=["']password["']""")
        self.assertIn("/staticfiles/css/styles.css", body)

    def test_admin_and_api_can_be_framed_by_this_origin(self):
        staff = User.objects.create_user(
            username="frame-staff",
            password="secret",
            is_staff=True,
        )
        self.client.force_login(staff)
        admin = self.client.get("/admin/", follow=False)
        api = self.client.get("/api/", follow=False)
        root = self.client.get("/", follow=False)
        self.assertEqual(admin.status_code, 200)
        self.assertEqual(api.status_code, 200)
        self.assertEqual(root.status_code, 302)
        for response in (admin, api, root):
            self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_logout_rejects_get_and_accepts_post(self):
        User.objects.create_user(username="logout-user", password="secret")
        client = Client(enforce_csrf_checks=True)
        client.get("/accounts/login/")
        self.assertTrue(client.login(username="logout-user", password="secret"))
        token = client.cookies["csrftoken"].value

        denied = client.get("/accounts/logout/")
        self.assertEqual(denied.status_code, 405)

        # Same shape nginx uses: https, forwarded host, and the CSRF header.
        logged_out = client.post(
            "/accounts/logout/",
            HTTP_X_CSRFTOKEN=token,
            HTTP_ORIGIN="https://testserver",
            HTTP_X_FORWARDED_HOST="testserver",
            HTTP_X_FORWARDED_PROTO="https",
        )
        self.assertEqual(logged_out.status_code, 302)
        self.assertEqual(logged_out.url, "/")
        self.assertNotIn("_auth_user_id", client.session)

    def test_asgi_routes_ticket_tunnel_to_guacamole_consumer(self):
        from guacozy_server.asgi import application

        consumers = _ticket_tunnel_consumers(application)
        self.assertIn(GuacamoleConsumer, consumers)

    def test_static_credential_password_is_encrypted_at_rest(self):
        secret = "s3cret-value"
        cred = StaticCredentials.objects.create(
            name="enc-test",
            username="alice",
            password=secret,
        )
        cred.refresh_from_db()
        self.assertEqual(cred.password, secret)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT password FROM backend_staticcredentials WHERE credentials_ptr_id = %s",
                [cred.pk],
            )
            raw = cursor.fetchone()[0]
        self.assertNotIn(secret, raw)
        self.assertTrue(raw)


def _closed_local_port():
    """A localhost TCP port that refuses connections immediately."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class _QuietGuacd:
    """Speaks the guacd handshake, then sends nothing until asked."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.client = None
        self.mouse_seen = threading.Event()
        self._stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _read_instruction(self, conn):
        data = b""
        while b";" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return data
            data += chunk
        return data

    def _run(self):
        self.sock.settimeout(5)
        try:
            conn, _addr = self.sock.accept()
        except OSError:
            return
        self.client = conn
        try:
            self._read_instruction(conn)
            conn.sendall(b"4.args;")
            pending = b""
            while pending.count(b";") < 5:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                pending += chunk
            session = "$" + str(uuid.uuid4())
            ready = f"5.ready,{len(session)}.{session};".encode()
            conn.sendall(ready + b"3.nop;")
            conn.settimeout(0.2)
            while not self._stop:
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                if b"mouse" in chunk:
                    self.mouse_seen.set()
        except OSError:
            return

    def send(self, payload):
        self.client.sendall(payload)

    def close(self):
        self._stop = True
        for sock in (self.client, self.sock):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass
        self.thread.join(timeout=2)


class TicketTunnelConnectTests(TransactionTestCase):
    async def test_connect_reads_ticket_relations_without_blocking_the_event_loop(self):
        user = await User.objects.acreate(username="tunnel-owner")
        user.set_password("secret")
        await user.asave()
        guacd = await GuacdServer.objects.acreate(
            name="closed-guacd",
            hostname="127.0.0.1",
            port=_closed_local_port(),
        )
        conn = await ConnectionSsh.objects.acreate(
            name="tunnel-ssh",
            host="127.0.0.1",
            guacdserver=guacd,
        )
        ticket = await Ticket.objects.acreate(
            connection=conn,
            user=user,
            author=user,
        )
        consumer = GuacamoleConsumer()
        # Channels assigns scope before it calls connect().
        consumer.scope = {
            "type": "websocket",
            "subprotocols": ["guacamole"],
            "query_string": b"width=800&height=600",
            "url_route": {"kwargs": {"ticket": ticket.id}},
            "user": user,
            "session": {},
        }

        # Handshake reaches the closed guacd only after the owner, guacd
        # server, and connection-parameter reads have run.
        with self.assertRaises(OSError) as raised:
            await consumer.connect()
        self.assertNotIsInstance(raised.exception, SynchronousOnlyOperation)
        self.assertTrue(await Ticket.objects.filter(pk=ticket.pk).aexists())

    async def test_input_is_forwarded_while_guacd_is_quiet(self):
        # create_connection's timeout used to stay on the socket, and
        # sync_to_async ran send and receive on one thread. A quiet guacd
        # then blocked mouse and key events until recv timed out.
        fake = _QuietGuacd()
        communicator = None
        user = await User.objects.acreate(username="tunnel-input")
        user.set_password("secret")
        await user.asave()
        guacd = await GuacdServer.objects.acreate(
            name="quiet-guacd",
            hostname="127.0.0.1",
            port=fake.port,
        )
        conn = await ConnectionSsh.objects.acreate(
            name="tunnel-input-ssh",
            host="127.0.0.1",
            guacdserver=guacd,
        )
        ticket = await Ticket.objects.acreate(
            connection=conn,
            user=user,
            author=user,
        )
        router = URLRouter([
            path("tunnelws/ticket/<uuid:ticket>/", GuacamoleConsumer.as_asgi()),
        ])

        async def application(scope, receive, send):
            scope["user"] = user
            scope["session"] = {}
            scope["client"] = ("127.0.0.1", 0)
            await router(scope, receive, send)

        original_init = GuacamoleClient.__init__

        def fast_timeout(self, host, port, timeout=20, debug=False, logger=None):
            original_init(self, host, port, timeout=0.4, debug=debug, logger=logger)

        try:
            with patch.object(GuacamoleClient, "__init__", fast_timeout):
                communicator = WebsocketCommunicator(
                    application,
                    f"/tunnelws/ticket/{ticket.id}/?width=800&height=600",
                    subprotocols=["guacamole"],
                )
                connected, _protocol = await communicator.connect(timeout=5)
                self.assertTrue(connected)
                self.assertEqual(await communicator.receive_from(timeout=2), "3.nop;")
                await asyncio.sleep(0.6)
                await communicator.send_to(text_data="5.mouse,3.100,3.200,1.0;")
                self.assertTrue(await asyncio.to_thread(fake.mouse_seen.wait, 2))
                fake.send(b"4.sync,1.1;")
                self.assertEqual(await communicator.receive_from(timeout=2), "4.sync,1.1;")
        finally:
            if communicator is not None:
                await communicator.disconnect(timeout=3)
            fake.close()
