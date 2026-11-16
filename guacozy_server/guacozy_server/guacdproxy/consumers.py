import asyncio
import logging
import socket
import uuid
from urllib.parse import parse_qsl

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from guacamole.client import GuacamoleClient
from guacamole.exceptions import GuacamoleError
from guacamole.instruction import GuacamoleInstruction

from backend.models import Ticket, Connection, AppSettings, TicketLog

logger = logging.getLogger(__name__)


class TunnelClient(GuacamoleClient):
    """Guacamole socket used as a long-lived tunnel.

    create_connection() leaves the connect timeout on every later recv.
    A quiet desktop then raises TimeoutError and the poller dies.
    send() and receive() also have to run on different threads: the
    default sync_to_async executor is a single thread, so a blocking
    recv would hold mouse, key, and sync instructions until it returns.
    """

    def relax_read_timeout(self):
        if self._client is not None:
            self._client.settimeout(None)

    def close(self):
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


class GuacamoleConsumer(AsyncWebsocketConsumer):
    gclient = None
    ticket = None
    allow_control = False

    async def connect(self):
        """
        Handles Websocket connect event
        Given ticket uuid should 
        - initiate connection with guacd using parameters from ticket's connection
        - accept websocket connection and start infinite polling loop proxying guacamole instructions between
          browser guacamole client ant guacd server 
        """

        # check if client indicated 'guacamole' as supported subprotocols
        if 'guacamole' not in self.scope['subprotocols']:
            await self.close()
            return

        params = {'audio': []}

        parsed_query = parse_qsl(self.scope["query_string"].decode('utf8'))

        for p in parsed_query:
            if p[0] == 'width':
                params['width'] = p[1]
            if p[0] == 'height':
                params['height'] = p[1]
            if p[0] == 'audio':
                params['audio'] = [p[1]]

        try:
            ticket = await database_sync_to_async(Ticket.objects.get)(id=self.scope["url_route"]["kwargs"]["ticket"])
            self.allow_control = ticket.control
        except Ticket.DoesNotExist:
            # https://guacamole.apache.org/doc/gug/protocol-reference.html#status-codes
            # 771	CLIENT_FORBIDDEN
            # Permission was denied, and logging in will not solve the problem.
            await self.accept_and_send_error("Ticket does not exist", 771)
            return

        # check that user is the proper owner of ticket
        if not await self.user_owns_ticket(ticket, self.scope['user']):
            # https://guacamole.apache.org/doc/gug/protocol-reference.html#status-codes
            # 771	CLIENT_FORBIDDEN
            # Permission was denied, and logging in will not solve the problem.
            await self.accept_and_send_error("You are not allowed to use this ticket", 771)
            return

        # check ticket validity and do not proceed if it is not valid
        if not ticket.check_validity():
            # https://guacamole.apache.org/doc/gug/protocol-reference.html#status-codes
            # 523	SESSION_CLOSED
            # The session within the upstream server has been forcibly closed.
            await self.accept_and_send_error("Ticket is no longer valid", 523)
            return

        sessionid = await self.get_ticket_sessionid(ticket)

        connection = await self.get_ticket_connection(ticket)

        # Try getting guacd server from connection
        guacd = await self.get_connection_guacd(connection)

        # If connection has no guacd specified, try default
        if guacd is None:
            guacd = await self.get_default_guacd_server()

        if guacd is None:
            await self.accept_and_send_error(
                "Connection has no guacd server specified "
                "and no default guacd server specified in app settings", 512)
            return None

        self.gclient = TunnelClient(guacd.hostname, guacd.port)

        # Check if ticket has session id.
        # This means that session is already in progress and we connect to it
        if sessionid:
            try:
                await self._guacd(self.gclient.handshake)(connectionid="$" + sessionid.__str__(), **params)
            except (AttributeError, GuacamoleError):
                # connection to existing sessioni failed
                # reset existing sessionid in ticket
                await self.update_ticket_sessionid(ticket, None)

        # If no existing session in progress (or reconnecting to it failed,
        # indicating as setting None above) we can try creating a new one
        sessionid = await self.get_ticket_sessionid(ticket)

        if not sessionid:
            # parameters = ticket.connection.get_guacamole_parameters()
            parameters = await self.get_guacamole_parameters(
                ticket, self.scope['user'])

            if parameters['passthrough_credentials']:
                try:
                    parameters['username'] = self.scope['session']['username']
                    parameters['password'] = self.scope['session']['password']
                except KeyError:
                    await self.accept_and_send_error(
                        "Exception! Username/passord not found in session.\n. "
                        , 999)

            if parameters['protocol'] == 'rdp' \
                    and (not parameters['password'] or not parameters['username']) \
                    and parameters['security'] not in ['rdp', 'tls']:
                await self.accept_and_send_error(
                    "No credentials found for this connection \n. "
                    "If you want to use RDP login screen, "
                    "please explicitly specify security as RDP or TLS", 999)

            await self._guacd(self.gclient.handshake)(**parameters, **params)
            await self.update_ticket_sessionid(ticket, self.gclient.id)

        #  If we have successfully connected,
        #  Guacamole Client should have an instance id
        if not self.gclient.id:
            await self.close()
            return

        await self._guacd(self.gclient.relax_read_timeout)()

        # Save ticket reference (so we can update on disconnect) and log event
        self.ticket = ticket
        await self.log_ticket_action(ticket, 'connect', self.scope)

        # start polling loop function,
        self._polling = asyncio.create_task(self.data_polling())
        self._polling.add_done_callback(self._polling_finished)

        # After we finish handshake and initiated polling loop
        # we accept connection
        await self.accept(subprotocol='guacamole')

    def _guacd(self, func):
        # Not thread-sensitive: receive blocks, and send must still run.
        return sync_to_async(func, thread_sensitive=False)

    def _polling_finished(self, task):
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("Guacamole poller stopped", exc_info=error)

    async def disconnect(self, code):
        """
        Websocket disconnect event handler, which closes session with guacd on  websocket disconnect
        """
        # socket disconnected - inform server that we are out
        if self.ticket is not None:
            await self.log_ticket_action(self.ticket, 'disconnect', self.scope)
        if self.gclient is not None:
            await self._guacd(self.gclient.close)()
        polling = getattr(self, "_polling", None)
        if polling is not None and not polling.done():
            polling.cancel()
            try:
                await polling
            except asyncio.CancelledError:
                pass

    async def accept_and_send_error(self, error_text, error_code):
        """
        Handle errors during connect with a reason and code
        In order for client side guacamole client to receive error code/text
        we need to accept connection and immediately send error instruction
        """
        await self.accept(subprotocol='guacamole')
        await self.send(text_data=GuacamoleInstruction("error", error_text,
                                                       error_code).encode())

    async def receive(self, text_data=None, bytes_data=None):
        """
        Webscocket receive even handler - send everything to associated guacd session
        """
        if text_data is None:
            return
        if self.allow_control is False and (text_data[0:7] == "5.mouse" or text_data[0:5] == "3.key"):
            return
        await self._guacd(self.gclient.send)(text_data)

    async def data_polling(self):
        """
        Polling loop - receives data from GuacamoleClient and passes to websocket
        """
        while True:
            try:
                content = await self._guacd(self.gclient.receive)()
            except OSError:
                return
            if content:
                # this is dirty workaround for problem.
                # If we reconnect, servers sends layer size 0x0
                # after this it sends normal size, but this causes
                # GuacamoleClient to update redraw "olddata"
                # which has become for a moment 0x0. Ant this causes
                #
                # Uncaught DOMException:
                # Failed to execute 'drawImage' on 'CanvasRenderingContext2D':
                #
                # The image argument is a canvas element with a width or
                # height of 0. so if content is "Set size of layer 1 to 0x0"
                # we just skip this command.
                if content == "4.size,1.1,1.0,1.0;":
                    continue
                await self.send(text_data=content)
            else:
                return

    @database_sync_to_async
    def user_owns_ticket(self, ticket, user):
        return ticket.user == user

    @database_sync_to_async
    def get_connection_guacd(self, connection):
        return connection.guacdserver

    @database_sync_to_async
    def get_guacamole_parameters(self, ticket, user):
        return Connection.objects.get(
            pk=ticket.connection.pk).get_guacamole_parameters(user)

    @database_sync_to_async
    def update_ticket_sessionid(self, ticket, sessionid):
        """
        Update session id of ticket or it's parent in case this is a shared ticket
        """
        if not ticket.parent:
            ticket_to_update = ticket
        else:
            ticket_to_update = ticket.parent

        if sessionid is None:
            ticket_to_update.sessionid = None
        else:
            # remove first character, because guacd sessionid has '$' as it's first symbol
            newuuid = sessionid.__str__()[1:37]
            ticket_to_update.sessionid = uuid.UUID(newuuid)

        ticket_to_update.save()

    @database_sync_to_async
    def log_ticket_action(self, ticket, action, scope):
        TicketLog.addlog(ticket, action, scope=scope)

    @database_sync_to_async
    def get_ticket_sessionid(self, ticket):
        """
        Gets session id associated with ticket or with it's parent if this is a shared ticket
        """
        if not ticket.parent:
            return ticket.sessionid
        return ticket.parent.sessionid

    @database_sync_to_async
    def get_ticket_connection(self, ticket):
        """
        Get connection definition from ticket or from tickets's parent if this is a shared ticket
        """
        if not ticket.parent:
            return ticket.connection
        return ticket.parent.connection

    @database_sync_to_async
    def get_default_guacd_server(self):
        return AppSettings.load().default_guacd_server
