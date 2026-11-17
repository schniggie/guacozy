import base64
import logging
import re
import socket
import threading
import uuid

from django.utils import timezone
from guacamole.client import GuacamoleClient
from guacamole.exceptions import GuacamoleError, InvalidInstruction
from guacamole.instruction import GuacamoleInstruction

from backend.agent.errors import fail, ok
from backend.agent.frame import Frame
from backend.agent.management import (
    _denied,
    _invalid,
    _missing,
    _owned_ticket,
    _owned_tickets,
    _tool,
    _unexpected,
    _visible_connection,
)
from backend.models import AppSettings, Connection, Ticket, TicketLog

# Sent on every handshake, including a join. A size after ready would resize
# the shared desktop. guacd's first args name is a protocol token; answer with
# the newest version this client implements, and never a higher one.
_WIDTH = 1280
_HEIGHT = 720
_DPI = 96
_SUPPORTED_VERSION = (1, 5, 0)
_VERSION = re.compile(r"^VERSION_(\d+)_(\d+)_(\d+)$")
logger = logging.getLogger("backend.agent")
_MAX_CLICKS = 5
_MAX_TYPE = 2000
_NOTE = "Pixels, origin top left. There are no element refs."
_NO_SESSION_PASSWORD = "The agent has no browser session password."
_BUTTONS = {"left": 1, "right": 4, "middle": 2}
_NAMED_KEYS = {
    "Enter": 0xff0d,
    "Tab": 0xff09,
    "Backspace": 0xff08,
    "Escape": 0xff1b,
    "Left": 0xff51,
    "Up": 0xff52,
    "Right": 0xff53,
    "Down": 0xff54,
    "Shift": 0xffe1,
    "Control": 0xffe3,
    "Alt": 0xffe9,
}

_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def _version_reply(token):
    match = _VERSION.match(token)
    if match is None:
        return None
    offered = tuple(int(part) for part in match.groups())
    chosen = offered if offered <= _SUPPORTED_VERSION else _SUPPORTED_VERSION
    return "VERSION_%d_%d_%d" % chosen


def _stale(retry):
    return fail("STALE_SESSION", "The desktop session is gone.", retry)


def _remote():
    return fail("REMOTE_DESKTOP", "Could not open the remote desktop.", "unsafe")


class AgentClient(GuacamoleClient):
    """Handshake answers the protocol version and always sends size. Sends are not logged."""

    def __init__(self, host, port, timeout=20, debug=False, logger=None):
        super().__init__(host, port, timeout=timeout, debug=debug, logger=logger)
        self._shut = False
        self._opened = False

    def relax_read_timeout(self):
        # create_connection leaves the connect timeout on later reads.
        if self._client is not None:
            self._client.settimeout(None)

    def close(self):
        # Idempotent. Never reads the client property: that opens a new TCP
        # connection when _client is None.
        self._shut = True
        sock = self._client
        self._client = None
        self.connected = False
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def send(self, data):
        if self._shut:
            raise OSError("desktop socket is closed")
        sock = self._client
        if sock is None:
            # First connect only. Once a socket has existed, do not open another.
            if self._opened:
                raise OSError("desktop socket is closed")
            sock = self.client
            self._opened = True
        # Instruction bodies, clipboard text, and connect args stay off the log.
        sock.sendall(data.encode())

    def send_instruction(self, instruction):
        self.send(instruction.encode())

    def receive(self):
        # Same buffer loop as GuacamoleClient.receive, without logging the body.
        start = 0
        while not self._shut:
            idx = self._buffer.find(b";", start)
            if idx != -1:
                line = self._buffer[:idx + 1].decode()
                self._buffer = self._buffer[idx + 1:]
                return line
            start = len(self._buffer)
            sock = self._client
            if sock is None:
                return None
            buf = sock.recv(4096)
            if not buf:
                self.close()
                return None
            self._buffer.extend(buf)
        return None

    def _read_instruction(self):
        raw = self.receive()
        if not raw:
            raise GuacamoleError("Connection lost")
        return GuacamoleInstruction.load(raw)

    def handshake(self, protocol="vnc", **kwargs):
        # width, audio, and image are client handshake fields, not connect args.
        for name in (
            "audio", "video", "image", "width", "height", "dpi",
            "width_override", "height_override", "dpi_override",
        ):
            kwargs.pop(name, None)
        if "connectionid" in kwargs:
            self.send_instruction(GuacamoleInstruction("select", kwargs["connectionid"]))
        else:
            if protocol not in ("vnc", "rdp", "ssh"):
                raise GuacamoleError("Missing protocol or connectionid.")
            self.send_instruction(GuacamoleInstruction("select", protocol))

        args_instruction = self._read_instruction()
        if args_instruction.opcode != "args":
            self.close()
            raise GuacamoleError("Expected args")

        connection_args = []
        for index, arg in enumerate(args_instruction.args):
            if index == 0:
                version = _version_reply(arg)
                if version is not None:
                    connection_args.append(version)
                    continue
            connection_args.append(kwargs.get(arg.replace("-", "_"), ""))
        # Size is required on a join. Without it guacd leaves the joiner at
        # 0x0 and does not replay the screen that is already open.
        self.send_instruction(GuacamoleInstruction("size", _WIDTH, _HEIGHT, _DPI))
        self.send_instruction(GuacamoleInstruction("audio"))
        self.send_instruction(GuacamoleInstruction("video"))
        self.send_instruction(GuacamoleInstruction("image", "image/png", "image/jpeg"))
        self.send_instruction(GuacamoleInstruction("connect", *connection_args))

        ready = self._read_instruction()
        if ready.opcode != "ready" or not ready.args:
            self.close()
            raise GuacamoleError("Expected ready")
        self._id = ready.args[0]
        self.connected = True
        self.relax_read_timeout()


class _Session:
    def __init__(self, user_id, client, control):
        self.id = str(uuid.uuid4())
        self.user_id = user_id
        self.client = client
        self.control = bool(control)
        self.frame = Frame()
        self.clipboard = ""
        self.stream_index = 0
        self.dead = False
        self._clean_shutdown = False
        self.generation = 0
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.published = threading.Condition(self.state_lock)
        self.thread = threading.Thread(target=self._receive, name="guacozy-desktop", daemon=True)

    def start(self):
        self.thread.start()

    def mark_dead(self):
        with self.state_lock:
            self.dead = True
            self.published.notify_all()

    def shutdown(self):
        # This socket only. The guacd session id on the ticket stays.
        # Set before close so the reader does not treat this as a dead peer.
        self._clean_shutdown = True
        self.client.close()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
        self.mark_dead()

    def _receive(self):
        try:
            while not self.client._shut:
                try:
                    raw = self.client.receive()
                except OSError:
                    return
                if not raw:
                    return
                try:
                    instruction = GuacamoleInstruction.load(raw)
                except (InvalidInstruction, UnicodeError):
                    logger.warning("desktop reader stopped on an unreadable instruction")
                    return
                if instruction.opcode == "sync":
                    # Reply before publishing so a snapshot cannot observe an unacked frame.
                    try:
                        with self.send_lock:
                            self.client.send_instruction(
                                GuacamoleInstruction("sync", *instruction.args)
                            )
                    except OSError:
                        return
                    with self.state_lock:
                        self.generation += 1
                        self.published.notify_all()
                    continue
                # error and disconnect end the remote session. One bad drawing
                # instruction must not, or the desktop dies on the next frame.
                if instruction.opcode in ("error", "disconnect"):
                    logger.warning("desktop reader stopped on %s", instruction.opcode)
                    return
                try:
                    with self.state_lock:
                        self.frame.apply(instruction)
                except Exception:
                    logger.warning("desktop reader skipped %s", instruction.opcode)
                    continue
        finally:
            if not self._clean_shutdown:
                self.client.close()
                self.mark_dead()


def _session_owner(ticket):
    # Parent session id wins, matching GuacamoleConsumer.get_ticket_sessionid.
    if ticket.parent_id:
        ticket.parent.refresh_from_db(fields=["sessionid"])
        return ticket.parent
    return ticket


def _joinable_ticket(user, connection):
    # The browser ticket already has the guacd id. A new handshake is a second
    # login, and Windows disconnects that browser with SESSION_CONFLICT.
    now = timezone.now()
    rows = list(
        _owned_tickets(user).filter(connection=connection).order_by("-created")
    )
    fallback = None
    for ticket in rows:
        if ticket.created + ticket.validityperiod < now:
            continue
        if _session_owner(ticket).sessionid is None:
            continue
        if ticket.control:
            return ticket
        if fallback is None:
            fallback = ticket
    return fallback


def _store_sessionid(ticket, guacd_id):
    owner = _session_owner(ticket)
    try:
        parsed = uuid.UUID(str(guacd_id)[1:37])
    except (TypeError, ValueError) as exc:
        raise GuacamoleError("Bad session id") from exc
    owner.sessionid = parsed
    owner.save(update_fields=["sessionid"])


def _clear_sessionid(ticket):
    owner = _session_owner(ticket)
    owner.sessionid = None
    owner.save(update_fields=["sessionid"])


def _handshake_new(host, port, connection, user, ticket):
    # Passwords live in this dict. Do not log it.
    parameters = Connection.objects.get(pk=connection.pk).get_guacamole_parameters(user)
    client = AgentClient(host, port)
    try:
        client.handshake(**parameters)
        _store_sessionid(ticket, client.id)
    except (OSError, GuacamoleError, InvalidInstruction, UnicodeError):
        client.close()
        raise
    return client


def _guacd_address(connection):
    guacd = connection.guacdserver
    if guacd is None:
        guacd = AppSettings.load().default_guacd_server
    if guacd is None:
        raise GuacamoleError("No guacd server is configured.")
    return guacd.hostname, guacd.port


def _load_session(user, arguments, allowed):
    error = _unexpected(arguments, allowed)
    if error is not None:
        return error
    if "session_id" not in arguments:
        return _invalid("session_id is required.")
    session_id = arguments["session_id"]
    if not isinstance(session_id, str):
        return _invalid("session_id must be a string.")
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
    if session is None or session.user_id != user.pk:
        return _missing("Desktop session not found.")
    return session


def _require_int(arguments, name):
    if name not in arguments:
        return _invalid(name + " is required.")
    value = arguments[name]
    if isinstance(value, bool) or not isinstance(value, int):
        return _invalid(name + " must be an integer.")
    return value


def _peer_gone(session):
    with session.state_lock:
        if session.dead or session.client._shut:
            session.dead = True
            return True
        sock = session.client._client
        if sock is None:
            session.dead = True
            session.published.notify_all()
            return True
    # Peek does not consume. A connected socket with nothing queued is not dead.
    try:
        peeked = sock.recv(1, socket.MSG_DONTWAIT | socket.MSG_PEEK)
    except BlockingIOError:
        return session.dead
    except OSError:
        session.mark_dead()
        return True
    if not peeked:
        session.mark_dead()
        return True
    return session.dead


def _transmit(session, instructions):
    if session.dead or session.client._shut:
        return _stale("unsafe")
    try:
        with session.send_lock:
            if session.dead or session.client._shut:
                return _stale("unsafe")
            for instruction in instructions:
                session.client.send_instruction(instruction)
    except OSError:
        session.mark_dead()
        return _stale("unsafe")
    return None


def _keysym(key):
    if not isinstance(key, str):
        return None
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if len(key) == 1 and key.isascii() and (key.isalpha() or key.isdigit()):
        return ord(key)
    return None


def _view_only(session):
    if session.control:
        return None
    return _denied("This share cannot control the desktop.")


def open_desktop(user, arguments):
    error = _unexpected(arguments, ("connection_id", "ticket_id"))
    if error is not None:
        return error
    has_connection = "connection_id" in arguments
    has_ticket = "ticket_id" in arguments
    if has_connection == has_ticket:
        return _invalid("Exactly one of connection_id and ticket_id is required.")

    if has_connection:
        connection = _visible_connection(user, arguments["connection_id"])
        if isinstance(connection, dict):
            return connection
        if connection.passthrough_credentials:
            return _denied(_NO_SESSION_PASSWORD)
        # Model default lifetime is one day. Opening does not require staff.
        # Reuse the caller's open ticket. Deleting it on failure would drop
        # the browser's sidebar session.
        ticket = _joinable_ticket(user, connection)
        created = ticket is None
        if ticket is None:
            ticket = Ticket.objects.create(
                connection=connection,
                user=user,
                author=user,
                control=True,
                parent=None,
            )
    else:
        ticket = _owned_ticket(user, arguments["ticket_id"])
        if isinstance(ticket, dict):
            return ticket
        if not ticket.check_validity():
            return _missing("Ticket not found.")
        source = ticket.parent if ticket.parent_id else ticket
        connection = source.connection
        if connection.passthrough_credentials:
            return _denied(_NO_SESSION_PASSWORD)
        created = False
    existing = _session_owner(ticket).sessionid
    client = None
    joined = False
    try:
        host, port = _guacd_address(connection)
        if existing:
            client = AgentClient(host, port)
            try:
                client.handshake(connectionid="$" + str(existing))
                joined = True
            except (OSError, GuacamoleError, InvalidInstruction, UnicodeError):
                # Match GuacamoleConsumer: drop the dead join, then open a new session.
                client.close()
                client = None
                _clear_sessionid(ticket)
                client = _handshake_new(host, port, connection, user, ticket)
        else:
            client = _handshake_new(host, port, connection, user, ticket)
    except (OSError, GuacamoleError, InvalidInstruction, UnicodeError):
        if client is not None:
            client.close()
        if created:
            ticket.delete()
        return _remote()

    TicketLog.addlog(ticket, "connect")
    session = _Session(user.pk, client, ticket.control)
    with _SESSIONS_LOCK:
        _SESSIONS[session.id] = session
    session.start()
    if joined:
        with session.state_lock:
            width, height = session.frame.size
    else:
        width, height = _WIDTH, _HEIGHT
    return ok({
        "session_id": session.id,
        "ticket_id": str(ticket.id),
        "width": width,
        "height": height,
        "joined": joined,
    })


def snapshot(user, arguments):
    session = _load_session(user, arguments, ("session_id",))
    if isinstance(session, dict):
        return session
    if _peer_gone(session):
        return _stale("safe")
    with session.state_lock:
        if session.dead:
            return _stale("safe")
        width, height = session.frame.size
        cursor_x, cursor_y = session.frame.cursor
        try:
            png_b64 = base64.b64encode(session.frame.png_bytes()).decode("ascii")
        except ValueError:
            png_b64 = ""
    return ok({
        "session_id": session.id,
        "width": width,
        "height": height,
        "png_base64": png_b64,
        "cursor": {"x": cursor_x, "y": cursor_y},
        "note": _NOTE,
    })


def click(user, arguments):
    session = _load_session(user, arguments, ("session_id", "x", "y", "button", "count"))
    if isinstance(session, dict):
        return session
    x = _require_int(arguments, "x")
    if isinstance(x, dict):
        return x
    y = _require_int(arguments, "y")
    if isinstance(y, dict):
        return y
    button = arguments.get("button", "left")
    if button not in _BUTTONS:
        return _invalid("button must be left, right, or middle.")
    if "count" in arguments:
        count = _require_int(arguments, "count")
        if isinstance(count, dict):
            return count
        if count < 1 or count > _MAX_CLICKS:
            return _invalid("count must be from 1 to 5.")
    else:
        count = 1
    denied = _view_only(session)
    if denied is not None:
        return denied
    mask = _BUTTONS[button]
    instructions = []
    for _ in range(count):
        instructions.append(GuacamoleInstruction("mouse", str(x), str(y), str(mask)))
        instructions.append(GuacamoleInstruction("mouse", str(x), str(y), "0"))
    error = _transmit(session, instructions)
    if error is not None:
        return error
    return ok({"session_id": session.id})


def type_text(user, arguments):
    session = _load_session(user, arguments, ("session_id", "text"))
    if isinstance(session, dict):
        return session
    text = arguments.get("text")
    if not isinstance(text, str):
        return _invalid("text must be a string.")
    if len(text) > _MAX_TYPE:
        return _invalid("text must be at most 2000 characters.")
    denied = _view_only(session)
    if denied is not None:
        return denied
    instructions = []
    for character in text:
        keysym = str(ord(character))
        instructions.append(GuacamoleInstruction("key", keysym, "1"))
        instructions.append(GuacamoleInstruction("key", keysym, "0"))
    error = _transmit(session, instructions)
    if error is not None:
        return error
    return ok({"session_id": session.id})


def press(user, arguments):
    session = _load_session(user, arguments, ("session_id", "key"))
    if isinstance(session, dict):
        return session
    keysym = _keysym(arguments.get("key"))
    if keysym is None:
        return _invalid("key is not a supported key.")
    denied = _view_only(session)
    if denied is not None:
        return denied
    symbol = str(keysym)
    error = _transmit(session, [
        GuacamoleInstruction("key", symbol, "1"),
        GuacamoleInstruction("key", symbol, "0"),
    ])
    if error is not None:
        return error
    return ok({"session_id": session.id})


def scroll(user, arguments):
    session = _load_session(user, arguments, ("session_id", "x", "y", "delta"))
    if isinstance(session, dict):
        return session
    x = _require_int(arguments, "x")
    if isinstance(x, dict):
        return x
    y = _require_int(arguments, "y")
    if isinstance(y, dict):
        return y
    delta = _require_int(arguments, "delta")
    if isinstance(delta, dict):
        return delta
    denied = _view_only(session)
    if denied is not None:
        return denied
    steps = min(abs(delta), 5)
    mask = "8" if delta > 0 else "16"
    instructions = []
    for _ in range(steps):
        instructions.append(GuacamoleInstruction("mouse", str(x), str(y), mask))
        instructions.append(GuacamoleInstruction("mouse", str(x), str(y), "0"))
    error = _transmit(session, instructions)
    if error is not None:
        return error
    return ok({"session_id": session.id})


def clipboard_get(user, arguments):
    session = _load_session(user, arguments, ("session_id",))
    if isinstance(session, dict):
        return session
    if _peer_gone(session):
        return _stale("unsafe")
    return ok({"text": session.clipboard})


def clipboard_set(user, arguments):
    session = _load_session(user, arguments, ("session_id", "text"))
    if isinstance(session, dict):
        return session
    text = arguments.get("text")
    if not isinstance(text, str):
        return _invalid("text must be a string.")
    denied = _view_only(session)
    if denied is not None:
        return denied
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if session.dead or session.client._shut:
        return _stale("unsafe")
    try:
        with session.send_lock:
            if session.dead or session.client._shut:
                return _stale("unsafe")
            stream = str(session.stream_index)
            session.stream_index += 1
            session.client.send_instruction(GuacamoleInstruction("clipboard", stream, "text/plain"))
            session.client.send_instruction(GuacamoleInstruction("blob", stream, encoded))
            session.client.send_instruction(GuacamoleInstruction("end", stream))
    except OSError:
        session.mark_dead()
        return _stale("unsafe")
    session.clipboard = text
    return ok({"session_id": session.id})


def close_desktop(user, arguments):
    error = _unexpected(arguments, ("session_id",))
    if error is not None:
        return error
    if "session_id" not in arguments:
        return _invalid("session_id is required.")
    session_id = arguments["session_id"]
    if not isinstance(session_id, str):
        return _invalid("session_id must be a string.")
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
        if session is None or session.user_id != user.pk:
            return _missing("Desktop session not found.")
        del _SESSIONS[session_id]
    session.shutdown()
    return ok({"session_id": session_id})


_SESSION = {"type": "string"}

DESKTOP_TOOLS = {
    "guacozy_open_desktop": _tool(
        "Open a visible connection or a ticket this user authors or holds. Shares the guacd session.",
        {
            "connection_id": {"type": "string"},
            "ticket_id": {"type": "string"},
        },
        None,
        open_desktop,
    ),
    "guacozy_snapshot": _tool(
        "Return the current frame as a PNG. Origin is top left. There are no element refs.",
        {"session_id": _SESSION},
        ("session_id",),
        snapshot,
    ),
    "guacozy_click": _tool(
        "Click at a pixel from the latest snapshot. Each count is mouse down then up.",
        {
            "session_id": _SESSION,
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "count": {"type": "integer"},
        },
        ("session_id", "x", "y"),
        click,
    ),
    "guacozy_type": _tool(
        "Type text. Each character is a key down and then a key up.",
        {"session_id": _SESSION, "text": {"type": "string"}},
        ("session_id", "text"),
        type_text,
    ),
    "guacozy_press": _tool(
        "Press one key, then release it. Named keys, letters, and digits only.",
        {"session_id": _SESSION, "key": {"type": "string"}},
        ("session_id", "key"),
        press,
    ),
    "guacozy_scroll": _tool(
        "Scroll at a pixel. Positive is up, negative is down, capped at 5 clicks.",
        {
            "session_id": _SESSION,
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "delta": {"type": "integer"},
        },
        ("session_id", "x", "y", "delta"),
        scroll,
    ),
    "guacozy_clipboard_get": _tool(
        "Return the clipboard text last set on this desktop.",
        {"session_id": _SESSION},
        ("session_id",),
        clipboard_get,
    ),
    "guacozy_clipboard_set": _tool(
        "Send text to the remote clipboard and store it for the next get.",
        {"session_id": _SESSION, "text": {"type": "string"}},
        ("session_id", "text"),
        clipboard_set,
    ),
    "guacozy_close_desktop": _tool(
        "Close this agent socket. The shared guacd session id stays on the ticket.",
        {"session_id": _SESSION},
        ("session_id",),
        close_desktop,
    ),
}
