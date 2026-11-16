import json

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from backend.agent.errors import fail, ok
from backend.agent.management import ACCESS_TOOLS, MANAGEMENT_TOOLS
from backend.agent.tokens import authenticate_bearer

PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
DEFAULT_PROTOCOL_VERSION = "2025-03-26"


def whoami(user, arguments):
    return ok({
        "id": user.pk,
        "username": user.username,
        "is_staff": user.is_staff,
    })


# tools/list and call_tool both read this table.
TOOLS = {
    "guacozy_whoami": {
        "description": "Return the Guacozy user this token acts as.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": whoami,
    },
    **MANAGEMENT_TOOLS,
    **ACCESS_TOOLS,
}


def call_tool(user, name: str, arguments: dict) -> dict:
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return fail("INVALID_ARGS", "Tool name and arguments are required.", "never")
    tool = TOOLS.get(name)
    if tool is None:
        return fail("NOT_FOUND", "Unknown tool.", "never")
    return tool["handler"](user, arguments)


def _rpc(request_id, *, result=None, error=None, status=200):
    body = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return JsonResponse(body, status=status)


def _tool_result(envelope):
    return {
        "content": [{"type": "text", "text": json.dumps(envelope)}],
        "isError": not envelope["ok"],
    }


@csrf_exempt
@require_POST
def mcp_view(request):
    # Bearer only. A session cookie must not authorize this path.
    user = authenticate_bearer(request.headers.get("Authorization"))
    if user is None:
        return JsonResponse(
            fail(
                "NOT_ALLOWED",
                "Authentication credentials were not provided.",
                "never",
            ),
            status=401,
        )

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _rpc(None, error={"code": -32700, "message": "Parse error"}, status=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("method"), str):
        request_id = payload.get("id") if isinstance(payload, dict) else None
        return _rpc(request_id, error={"code": -32600, "message": "Invalid Request"})

    request_id = payload.get("id")
    method = payload["method"]
    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _rpc(request_id, error={"code": -32602, "message": "Invalid params"})

    if method == "notifications/initialized":
        return HttpResponse(status=202)

    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol_version = requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return _rpc(request_id, result={
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "guacozy", "version": "1.0.0"},
        })

    if method == "tools/list":
        return _rpc(request_id, result={
            "tools": [
                {
                    "name": name,
                    "description": tool["description"],
                    "inputSchema": tool["inputSchema"],
                }
                for name, tool in TOOLS.items()
            ],
        })

    if method == "tools/call":
        envelope = call_tool(user, params.get("name"), params.get("arguments", {}))
        return _rpc(request_id, result=_tool_result(envelope))

    return _rpc(request_id, error={"code": -32601, "message": "Method not found"})
