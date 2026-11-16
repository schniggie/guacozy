import logging
import uuid
from datetime import timedelta

from django.contrib.auth.models import Group
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import DataError, IntegrityError, transaction
from django.db.models import BooleanField, CharField, IntegerField, Q, TextField
from django.utils import timezone
from mptt.exceptions import InvalidMove

from backend.agent.errors import fail, ok
from backend.api.utils import user_allowed_folders_ids
from backend.models import (
    Connection,
    ConnectionRdp,
    ConnectionSsh,
    ConnectionVnc,
    Folder,
    FolderPermission,
    GuacdServer,
    Ticket,
)
from users.models import User

logger = logging.getLogger(__name__)

PROTOCOL_MODELS = {
    "rdp": ConnectionRdp,
    "vnc": ConnectionVnc,
    "ssh": ConnectionSsh,
}

SHARED_FIELDS = (
    "port",
    "username",
    "password",
    "domain",
    "passthrough_credentials",
    "guacdserver",
)

# Returned for every connection, then the non-secret fields of that child.
PUBLIC_CHILD_FIELDS = {
    "rdp": (
        "ignore_cert",
        "security",
        "disable_auth",
        "console",
        "initial_program",
        "server_layout",
        "color_depth",
        "resize_method",
    ),
    "vnc": (
        "autoretry",
        "color_depth",
        "swap_red_blue",
        "cursor_remote",
        "read_only",
        "repeater_dest_host",
        "repeater_dest_port",
        "reverse_connect",
        "listen_timeout",
        "clipboard_encoding",
    ),
    "ssh": (
        "host_key",
        "server_alive_interval",
        "font_size",
        "command",
    ),
}

CHILD_INPUT_FIELDS = {
    "rdp": PUBLIC_CHILD_FIELDS["rdp"],
    "vnc": PUBLIC_CHILD_FIELDS["vnc"],
    "ssh": PUBLIC_CHILD_FIELDS["ssh"] + ("private_key", "passphrase"),
}

# The required RDP create sends private_key. It is an SSH column, so drop it
# when the child model has no such column. passphrase stays SSH-only and is
# INVALID_ARGS on RDP and VNC. Any other unknown field is rejected.
IGNORED_UNLESS_PRESENT = ("private_key",)

_IDENTITY_KEYS = {"protocol", "name", "host", "parent", "connection_id"}

_FIELD_SCHEMA = {
    "port": {"type": "integer"},
    "username": {"type": "string"},
    "password": {
        "type": "string",
        "description": "Write-only. Blank on update does not clear the stored password.",
    },
    "domain": {"type": "string"},
    "passthrough_credentials": {"type": "boolean"},
    "guacdserver": {"type": "integer"},
    "ignore_cert": {"type": "boolean"},
    "security": {"type": "string"},
    "disable_auth": {"type": "boolean"},
    "console": {"type": "boolean"},
    "initial_program": {"type": "string"},
    "server_layout": {"type": "string"},
    "color_depth": {"type": "string"},
    "resize_method": {"type": "string"},
    "autoretry": {"type": "integer"},
    "swap_red_blue": {"type": "boolean"},
    "cursor_remote": {"type": "boolean"},
    "read_only": {"type": "boolean"},
    "repeater_dest_host": {"type": "string"},
    "repeater_dest_port": {"type": "integer"},
    "reverse_connect": {"type": "boolean"},
    "listen_timeout": {"type": "integer"},
    "clipboard_encoding": {"type": "string"},
    "host_key": {"type": "string"},
    "server_alive_interval": {"type": "integer"},
    "private_key": {
        "type": "string",
        "description": "Write-only SSH private key. Ignored for RDP and VNC. Never returned.",
    },
    "passphrase": {
        "type": "string",
        "description": "Write-only SSH key passphrase. SSH only. Never returned.",
    },
    "font_size": {"type": "integer"},
    "command": {"type": "string"},
}


def _invalid(message):
    return fail("INVALID_ARGS", message, "never")


def _denied(message):
    return fail("NOT_ALLOWED", message, "never")


def _missing(message):
    return fail("NOT_FOUND", message, "never")


def _is_int_id(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _has_field(model, name):
    try:
        model._meta.get_field(name)
    except FieldDoesNotExist:
        return False
    return True


def _unexpected(arguments, allowed):
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        return _invalid("Unknown field: " + ", ".join(unknown) + ".")
    return None


def _viewable_ids(user):
    return user_allowed_folders_ids(user, require_view_permission=True)


def _listable_ids(user):
    return user_allowed_folders_ids(user, require_view_permission=False)


def _folder_payload(folder):
    return {"id": folder.id, "name": folder.name, "parent": folder.parent_id}


def _viewable_parent(user, parent_id, denied_message):
    if not _is_int_id(parent_id):
        return _invalid("parent must be a folder id.")
    # Same membership test as FolderFlatViewSet.perform_create: the parent
    # object must already be one the user can view. No staff root bypass.
    if parent_id not in _viewable_ids(user):
        return _denied(denied_message)
    return Folder.objects.get(pk=parent_id)


def _folder_for_change(user, folder_id, denied_message):
    # get_queryset is the list set. perform_update / perform_destroy then
    # require view permission, which excludes ancestors the user can only see
    # in order to reach a shared child.
    if folder_id not in _listable_ids(user):
        return _missing("Folder not found.")
    if folder_id not in _viewable_ids(user):
        return _denied(denied_message)
    return Folder.objects.get(pk=folder_id)


def _clean_value(field, value):
    label = field.name
    if isinstance(field, BooleanField):
        if not isinstance(value, bool):
            return None, _invalid(f"{label} is invalid.")
        return value, None
    if isinstance(field, IntegerField):
        if value is None:
            if field.null:
                return None, None
            return None, _invalid(f"{label} is invalid.")
        if isinstance(value, bool) or not isinstance(value, int):
            return None, _invalid(f"{label} is invalid.")
        return value, None
    if isinstance(field, (CharField, TextField)):
        if value is None:
            if field.null:
                return None, None
            return None, _invalid(f"{label} is invalid.")
        if not isinstance(value, str):
            return None, _invalid(f"{label} is invalid.")
        if field.max_length is not None and len(value) > field.max_length:
            return None, _invalid(f"{label} is invalid.")
        if value == "" and not field.blank:
            return None, _invalid(f"{label} is required.")
        if field.choices and value not in {choice[0] for choice in field.choices}:
            return None, _invalid(f"{label} is invalid.")
        return value, None
    return None, _invalid(f"{label} is invalid.")


def _assign_field(instance, field_name, value):
    cleaned, error = _clean_value(instance._meta.get_field(field_name), value)
    if error is not None:
        return error
    setattr(instance, field_name, cleaned)
    return None


def _assign_guacdserver(connection, value):
    if value is None:
        connection.guacdserver = None
        return None
    if not _is_int_id(value):
        return _invalid("guacdserver is invalid.")
    try:
        connection.guacdserver = GuacdServer.objects.get(pk=value)
    except GuacdServer.DoesNotExist:
        return _invalid("guacdserver is invalid.")
    return None


def _apply_optional(connection, arguments, *, updating):
    for key, value in arguments.items():
        if key in _IDENTITY_KEYS:
            continue
        if key in IGNORED_UNLESS_PRESENT and not _has_field(connection, key):
            continue
        # Connection.save restores a blank password from the value loaded in
        # __init__. Do not assign "" either, so that restore is not the only
        # thing keeping the stored password.
        if key == "password" and updating and (value is None or value == ""):
            continue
        if key == "guacdserver":
            error = _assign_guacdserver(connection, value)
            if error is not None:
                return error
            continue
        if not _has_field(connection, key):
            return _invalid("Unknown field.")
        error = _assign_field(connection, key, value)
        if error is not None:
            return error
    return None


def _input_fields(protocol):
    return set(SHARED_FIELDS) | set(CHILD_INPUT_FIELDS[protocol]) | set(IGNORED_UNLESS_PRESENT)


def _connection_payload(connection):
    data = {
        "id": str(connection.id),
        "name": connection.name,
        "protocol": connection.protocol,
        "parent": connection.parent_id,
        "host": connection.host,
        "port": connection.port,
        "username": connection.username,
        "domain": connection.domain,
        "passthrough_credentials": connection.passthrough_credentials,
        "has_password": bool(connection.password),
        "has_private_key": bool(getattr(connection, "private_key", None)),
        "guacdserver": connection.guacdserver_id,
    }
    for field_name in PUBLIC_CHILD_FIELDS[connection.protocol]:
        data[field_name] = getattr(connection, field_name)
    return data


def _visible_connection(user, connection_id):
    if not isinstance(connection_id, str):
        return _invalid("connection_id must be a string.")
    try:
        parsed = uuid.UUID(connection_id)
    except ValueError:
        return _invalid("connection_id must be a connection id.")
    try:
        connection = Connection.objects.get(pk=parsed, parent_id__in=_viewable_ids(user))
    except Connection.DoesNotExist:
        return _missing("Connection not found.")
    return connection.get_real_instance()


def _log_connection(tool_name, connection):
    logger.info("%s %s", tool_name, connection.id)


def _save_connection(connection):
    if connection.name:
        taken = Connection.objects.filter(name=connection.name)
        if connection.pk:
            taken = taken.exclude(pk=connection.pk)
        if taken.exists():
            return _invalid("name is already in use.")
    try:
        with transaction.atomic():
            connection.save()
            connection.refresh_from_db()
    except (IntegrityError, DataError, ValidationError, ValueError, TypeError):
        return _invalid("Could not save the connection.")
    return connection


def list_folders(user, arguments):
    error = _unexpected(arguments, ())
    if error is not None:
        return error
    folders = Folder.objects.filter(id__in=_listable_ids(user)).order_by("tree_id", "lft")
    return ok({"folders": [_folder_payload(folder) for folder in folders]})


def create_folder(user, arguments):
    error = _unexpected(arguments, ("name", "parent"))
    if error is not None:
        return error
    if "parent" not in arguments:
        return _invalid("parent is required.")
    if "name" not in arguments:
        return _invalid("name is required.")
    parent = _viewable_parent(user, arguments["parent"], "You are not allowed to create folder here")
    if isinstance(parent, dict):
        return parent
    folder = Folder(parent=parent)
    error = _assign_field(folder, "name", arguments["name"])
    if error is not None:
        return error
    try:
        folder.save()
    except InvalidMove:
        return _denied("You are not allowed to create folder here")
    return ok(_folder_payload(folder))


def update_folder(user, arguments):
    error = _unexpected(arguments, ("folder_id", "name", "parent"))
    if error is not None:
        return error
    if "folder_id" not in arguments:
        return _invalid("folder_id is required.")
    if not _is_int_id(arguments["folder_id"]):
        return _invalid("folder_id must be a folder id.")
    folder = _folder_for_change(
        user,
        arguments["folder_id"],
        "You are not allowed to update this folder",
    )
    if isinstance(folder, dict):
        return folder
    if "name" in arguments:
        error = _assign_field(folder, "name", arguments["name"])
        if error is not None:
            return error
    if "parent" in arguments:
        parent = _viewable_parent(user, arguments["parent"], "You are not allowed to move folder here")
        if isinstance(parent, dict):
            return parent
        folder.parent = parent
    try:
        folder.save()
    except InvalidMove:
        return _denied("You are not allowed to move folder here")
    return ok(_folder_payload(folder))


def delete_folder(user, arguments):
    error = _unexpected(arguments, ("folder_id",))
    if error is not None:
        return error
    if "folder_id" not in arguments:
        return _invalid("folder_id is required.")
    if not _is_int_id(arguments["folder_id"]):
        return _invalid("folder_id must be a folder id.")
    folder = _folder_for_change(
        user,
        arguments["folder_id"],
        "You are not allowed to delete this folder.",
    )
    if isinstance(folder, dict):
        return folder
    if folder.children.count() > 0 or folder.connections.count() > 0:
        return _denied("Cannot delete: folder is not empty.")
    payload = _folder_payload(folder)
    folder.delete()
    return ok(payload)


def list_connections(user, arguments):
    error = _unexpected(arguments, ())
    if error is not None:
        return error
    rows = Connection.objects.filter(parent_id__in=_viewable_ids(user)).order_by("name")
    return ok({
        "connections": [_connection_payload(row.get_real_instance()) for row in rows],
    })


def get_connection(user, arguments):
    error = _unexpected(arguments, ("connection_id",))
    if error is not None:
        return error
    if "connection_id" not in arguments:
        return _invalid("connection_id is required.")
    connection = _visible_connection(user, arguments["connection_id"])
    if isinstance(connection, dict):
        return connection
    return ok(_connection_payload(connection))


def create_connection(user, arguments):
    if any(key not in arguments for key in ("protocol", "name", "host", "parent")):
        return _invalid("protocol, name, host, and parent are required.")
    protocol = arguments["protocol"]
    if not isinstance(protocol, str) or protocol not in PROTOCOL_MODELS:
        return _invalid("protocol must be rdp, vnc, or ssh.")
    error = _unexpected(arguments, {"protocol", "name", "host", "parent"} | _input_fields(protocol))
    if error is not None:
        return error
    if not user.is_staff:
        return _denied("You are not allowed to create a connection.")
    parent = _viewable_parent(
        user,
        arguments["parent"],
        "You are not allowed to create a connection in this folder.",
    )
    if isinstance(parent, dict):
        return parent
    connection = PROTOCOL_MODELS[protocol](parent=parent)
    for field_name in ("name", "host"):
        error = _assign_field(connection, field_name, arguments[field_name])
        if error is not None:
            return error
    error = _apply_optional(connection, arguments, updating=False)
    if error is not None:
        return error
    saved = _save_connection(connection)
    if isinstance(saved, dict):
        return saved
    _log_connection("guacozy_create_connection", saved)
    return ok(_connection_payload(saved))


def update_connection(user, arguments):
    if "connection_id" not in arguments:
        return _invalid("connection_id is required.")
    connection = _visible_connection(user, arguments["connection_id"])
    if isinstance(connection, dict):
        return connection
    allowed = {"connection_id", "name", "host", "parent"} | _input_fields(connection.protocol)
    error = _unexpected(arguments, allowed)
    if error is not None:
        return error
    if not user.is_staff:
        return _denied("You are not allowed to change this connection.")
    for field_name in ("name", "host"):
        if field_name in arguments:
            error = _assign_field(connection, field_name, arguments[field_name])
            if error is not None:
                return error
    if "parent" in arguments:
        parent = _viewable_parent(
            user,
            arguments["parent"],
            "You are not allowed to move this connection here.",
        )
        if isinstance(parent, dict):
            return parent
        connection.parent = parent
    error = _apply_optional(connection, arguments, updating=True)
    if error is not None:
        return error
    saved = _save_connection(connection)
    if isinstance(saved, dict):
        return saved
    _log_connection("guacozy_update_connection", saved)
    return ok(_connection_payload(saved))


def delete_connection(user, arguments):
    error = _unexpected(arguments, ("connection_id",))
    if error is not None:
        return error
    if "connection_id" not in arguments:
        return _invalid("connection_id is required.")
    connection = _visible_connection(user, arguments["connection_id"])
    if isinstance(connection, dict):
        return connection
    if not user.is_staff:
        return _denied("You are not allowed to delete this connection.")
    payload = _connection_payload(connection)
    _log_connection("guacozy_delete_connection", connection)
    connection.delete()
    return ok(payload)


def _ticket_payload(ticket):
    # A row saved earlier can still sum past year 9999. Do not fail the list.
    try:
        validto = timezone.localtime(ticket.created + ticket.validityperiod).isoformat()
    except OverflowError:
        validto = None
    return {
        "id": str(ticket.id),
        "connection_id": str(ticket.connection_id),
        "control": ticket.control,
        "parent": None if ticket.parent_id is None else str(ticket.parent_id),
        "validto": validto,
    }


def _owned_tickets(user):
    # Same author-or-holder filter as TicketViewSet.get_queryset.
    # That viewset also deletes every user's expired rows. These tools do not.
    return Ticket.objects.filter(Q(user=user) | Q(author=user))


def _owned_ticket(user, ticket_id):
    if not isinstance(ticket_id, str):
        return _invalid("ticket_id must be a string.")
    try:
        parsed = uuid.UUID(ticket_id)
    except ValueError:
        return _invalid("ticket_id must be a ticket id.")
    try:
        return _owned_tickets(user).get(pk=parsed)
    except Ticket.DoesNotExist:
        return _missing("Ticket not found.")


def _one_principal(arguments):
    user_id = arguments.get("user_id")
    group_id = arguments.get("group_id")
    if (user_id is None) == (group_id is None):
        return _invalid("Exactly one of user_id and group_id is required.")
    if user_id is not None and not _is_int_id(user_id):
        return _invalid("user_id must be a user id.")
    if group_id is not None and not _is_int_id(group_id):
        return _invalid("group_id must be a group id.")
    return user_id, group_id


def set_folder_access(user, arguments):
    error = _unexpected(arguments, ("folder_id", "action", "user_id", "group_id"))
    if error is not None:
        return error
    if "folder_id" not in arguments:
        return _invalid("folder_id is required.")
    if not _is_int_id(arguments["folder_id"]):
        return _invalid("folder_id must be a folder id.")
    action = arguments.get("action")
    if action not in ("grant", "revoke"):
        return _invalid("action must be grant or revoke.")
    principal = _one_principal(arguments)
    if isinstance(principal, dict):
        return principal
    user_id, group_id = principal
    if not user.is_staff:
        return _denied("You are not allowed to change folder access.")
    # Staff grant, not the view list: any existing folder id is addressable.
    try:
        folder = Folder.objects.get(pk=arguments["folder_id"])
    except Folder.DoesNotExist:
        return _missing("Folder not found.")
    if user_id is not None:
        try:
            target_user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return _missing("User not found.")
        rows = FolderPermission.objects.filter(folder=folder, user=target_user)
        if action == "grant":
            if not rows.exists():
                FolderPermission.objects.create(folder=folder, user=target_user)
        else:
            rows.delete()
        granted_user = target_user.pk
        granted_group = None
    else:
        try:
            target_group = Group.objects.get(pk=group_id)
        except Group.DoesNotExist:
            return _missing("Group not found.")
        rows = FolderPermission.objects.filter(folder=folder, group=target_group)
        if action == "grant":
            if not rows.exists():
                FolderPermission.objects.create(folder=folder, group=target_group)
        else:
            rows.delete()
        granted_user = None
        granted_group = target_group.pk
    return ok({
        "folder_id": folder.id,
        "action": action,
        "user_id": granted_user,
        "group_id": granted_group,
    })


def list_tickets(user, arguments):
    error = _unexpected(arguments, ())
    if error is not None:
        return error
    tickets = _owned_tickets(user).order_by("created", "id")
    return ok({"tickets": [_ticket_payload(ticket) for ticket in tickets]})


def create_ticket(user, arguments):
    error = _unexpected(arguments, ("connection_id", "validity_seconds"))
    if error is not None:
        return error
    if "connection_id" not in arguments:
        return _invalid("connection_id is required.")
    seconds = arguments.get("validity_seconds", 86400)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
        return _invalid("validity_seconds must be a positive integer.")
    connection = _visible_connection(user, arguments["connection_id"])
    if isinstance(connection, dict):
        return connection
    try:
        period = timedelta(seconds=seconds)
        # Reject before insert. datetime cannot add a period past year 9999.
        timezone.now() + period
    except OverflowError:
        return _invalid("validity_seconds is invalid.")
    ticket = Ticket.objects.create(
        connection=connection,
        user=user,
        author=user,
        control=True,
        parent=None,
        validityperiod=period,
    )
    return ok(_ticket_payload(ticket))


def delete_ticket(user, arguments):
    error = _unexpected(arguments, ("ticket_id",))
    if error is not None:
        return error
    if "ticket_id" not in arguments:
        return _invalid("ticket_id is required.")
    ticket = _owned_ticket(user, arguments["ticket_id"])
    if isinstance(ticket, dict):
        return ticket
    payload = _ticket_payload(ticket)
    ticket.delete()
    return ok(payload)


def _schema(properties, required=None):
    schema = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _tool(description, properties, required, handler):
    return {
        "description": description,
        "inputSchema": _schema(properties, required),
        "handler": handler,
    }


_FOLDER_ID = {"type": "integer"}
_NAME = {"type": "string"}
_PARENT = {"type": "integer"}
_CONNECTION_FIELDS = {
    "name": _NAME,
    "host": {"type": "string"},
    "parent": _PARENT,
    **_FIELD_SCHEMA,
}


MANAGEMENT_TOOLS = {
    "guacozy_list_folders": _tool(
        "List folders this user may list.",
        {},
        None,
        list_folders,
    ),
    "guacozy_create_folder": _tool(
        "Create a folder inside a folder this user may view.",
        {"name": _NAME, "parent": _PARENT},
        ("name", "parent"),
        create_folder,
    ),
    "guacozy_update_folder": _tool(
        "Rename or move a folder this user may view.",
        {"folder_id": _FOLDER_ID, "name": _NAME, "parent": _PARENT},
        ("folder_id",),
        update_folder,
    ),
    "guacozy_delete_folder": _tool(
        "Delete an empty folder this user may view.",
        {"folder_id": _FOLDER_ID},
        ("folder_id",),
        delete_folder,
    ),
    "guacozy_list_connections": _tool(
        "List connections in folders this user may view.",
        {},
        None,
        list_connections,
    ),
    "guacozy_get_connection": _tool(
        "Fetch one connection in a folder this user may view.",
        {"connection_id": {"type": "string"}},
        ("connection_id",),
        get_connection,
    ),
    "guacozy_create_connection": _tool(
        "Create an RDP, VNC, or SSH connection. Staff only.",
        {
            "protocol": {"type": "string", "enum": ["rdp", "vnc", "ssh"]},
            **_CONNECTION_FIELDS,
        },
        ("protocol", "name", "host", "parent"),
        create_connection,
    ),
    "guacozy_update_connection": _tool(
        "Change a visible connection. Staff only.",
        {"connection_id": {"type": "string"}, **_CONNECTION_FIELDS},
        ("connection_id",),
        update_connection,
    ),
    "guacozy_delete_connection": _tool(
        "Delete a visible connection. Staff only.",
        {"connection_id": {"type": "string"}},
        ("connection_id",),
        delete_connection,
    ),
}


ACCESS_TOOLS = {
    "guacozy_set_folder_access": _tool(
        "Grant or revoke one user or one group on a folder. Staff only.",
        {
            "folder_id": _FOLDER_ID,
            "action": {"type": "string", "enum": ["grant", "revoke"]},
            "user_id": {"type": "integer"},
            "group_id": {"type": "integer"},
        },
        ("folder_id", "action"),
        set_folder_access,
    ),
    "guacozy_list_tickets": _tool(
        "List tickets this user authors or holds.",
        {},
        None,
        list_tickets,
    ),
    "guacozy_create_ticket": _tool(
        "Create a ticket for a visible connection. The caller is the author and the holder.",
        {
            "connection_id": {"type": "string"},
            "validity_seconds": {
                "type": "integer",
                "description": "Lifetime in seconds. Defaults to 86400.",
            },
        },
        ("connection_id",),
        create_ticket,
    ),
    "guacozy_delete_ticket": _tool(
        "Delete a ticket this user authors or holds.",
        {"ticket_id": {"type": "string"}},
        ("ticket_id",),
        delete_ticket,
    ),
}
