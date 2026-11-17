# Agent use of Guacozy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An outside agent signs in as the Guacozy user and can manage folders, connections, tickets, and folder access, and can look at and drive a shared RDP, VNC, or SSH session.

**Architecture:** A personal bearer token authenticates `POST /mcp/`, a small MCP streamable-HTTP handler in Django. Management tools call the existing models and the folder rules already in `backend/api/views.py`. Desktop tools keep a `TunnelClient` on the same guacd session id the browser joins, composite PNG frames in process, and send mouse and key instructions on that socket.

**Tech Stack:** Django 6.1.3, Django REST framework session auth for the token UI, existing `pyguacamole` `TunnelClient`, Pillow for PNG only, React start panel, existing `styles.css` tokens.

**Spec:** `docs/superpowers/specs/2026-11-16-agent-desktop-design.md`

## Global Constraints

- MCP is HTTP on the existing origin at `POST /mcp/`. No stdio server. No new nginx `location`: `location /` already proxies it.
- Bearer token only on `/mcp/`. Session cookies are not accepted there. The secret is shown once and stored as a SHA-256 hash. Missing, wrong, or revoked tokens are rejected.
- The agent is the token's user. Folder view rules still hide connections. `NOT_FOUND` when the object is missing or not visible. Do not reveal that a hidden object exists.
- Creating, editing, and deleting connections, and `guacozy_set_folder_access`, require `user.is_staff`. Opening a desktop and creating a ticket do not, when the user can already see the connection.
- Folder create, update, and delete keep the checks in `FolderFlatViewSet` (`backend/api/views.py`). Deleting a non-empty folder is refused.
- Do not turn `ConnectionViewSet` into a public write API. Writes go through the MCP tools.
- Connection payloads omit `password`, `private_key`, and `passphrase`. A blank password on update keeps the stored password.
- First client handshakes at 1280×720, dpi 96, image mime `image/png` only. A client that joins an existing session id does not send a `size` instruction.
- The agent client subclasses `TunnelClient`, calls `relax_read_timeout()` after a successful handshake, and sends and receives on different threads. Do not change `guacozy_server/guacdproxy/consumers.py`.
- Every `sync` instruction from guacd is answered with `sync` and the same timestamp. A client that only receives is the bug that already killed remote input.
- `guacozy_press` sends key down and then key up. There is no hold-down tool.
- Passwords, private keys, clipboard text, and screenshot bytes are never logged.
- Desktop sessions live in a process-local dict. They are not stored in the database and not shared across processes.
- Tool failures use `{"ok": false, "error": {"code", "message", "retry"}}`. Codes are only `INVALID_ARGS`, `NOT_ALLOWED`, `NOT_FOUND`, `STALE_SESSION`, `REMOTE_DESKTOP`. `retry` is only `safe`, `unsafe`, or `never`.
- Clicks, typing, key presses, scrolling, and clipboard writes are `unsafe` to retry. Permission and missing-object errors are `never`. A snapshot of a dead session is `safe`.
- There are no element refs. Snapshot text says pixels, origin top left.
- Do not rewrite MkDocs. Do not bump pins other than adding Pillow. Do not change login, the visual tokens, or the tunnel timeout fix.
- Tests run from `guacozy_server` with `../.venv/bin/python manage.py test`. Frontend build uses Node 26.13.1 from `frontend`.

## Review Focus

These are the failures a person would hit that no happy-path test covers unless a task pins them.

- A logged-in browser session posted to `/mcp/` without a bearer token must be rejected. Test: Task 2, `test_mcp_ignores_session_cookie`.
- A connection tool result must not contain `password`, `private_key`, or `passphrase`, including after create. Test: Task 3, `test_connection_payload_omits_secrets`.
- Joining a session that already has a session id must not send `size`. Test: Task 7, `test_join_does_not_send_size`.
- `guacozy_press` of a modifier must send key up after key down. Test: Task 7, `test_press_releases_key`.
- A click that fails is `retry: unsafe`. A permission failure is `retry: never`. Tests: Task 7 `test_click_failure_retry_is_unsafe`, and Task 3 `test_non_staff_cannot_create_connection`.

## File map

- Create `guacozy_server/backend/models/agenttoken.py` — token row.
- Modify `guacozy_server/backend/models/__init__.py` — export `AgentToken`.
- Create `guacozy_server/backend/migrations/0017_agenttoken.py`.
- Create `guacozy_server/backend/agent/__init__.py`.
- Create `guacozy_server/backend/agent/errors.py` — `ok` and `fail`.
- Create `guacozy_server/backend/agent/tokens.py` — hash, create, bearer lookup.
- Create `guacozy_server/backend/agent/management.py` — folder, connection, ticket, access tools.
- Create `guacozy_server/backend/agent/frame.py` — instruction compositor and PNG.
- Create `guacozy_server/backend/agent/desktop.py` — guacd session and desktop tools.
- Create `guacozy_server/backend/agent/mcp.py` — JSON-RPC view and tool registry.
- Modify `guacozy_server/guacozy_server/urls.py` — `mcp/` route.
- Modify `guacozy_server/backend/api/urls.py` and `backend/api/views.py` — session-auth token API for the start panel.
- Modify `frontend/src/Api/GuacozyApi.js`, `frontend/src/Context/AppContext.jsx`, `frontend/src/Components/Welcome/Welcome.jsx`, `guacozy_server/static/css/styles.css`.
- Modify `guacozy_server/requirements-base.txt` — pin Pillow.
- Modify `AGENTS.md`.
- Create `guacozy_server/backend/test_agent.py` — all new Django tests.

---

### Task 1: Agent tokens

**Files:**
- Create: `guacozy_server/backend/models/agenttoken.py`
- Create: `guacozy_server/backend/migrations/0017_agenttoken.py`
- Modify: `guacozy_server/backend/models/__init__.py`
- Create: `guacozy_server/backend/agent/__init__.py`
- Create: `guacozy_server/backend/agent/tokens.py`
- Modify: `guacozy_server/backend/api/views.py`
- Modify: `guacozy_server/backend/api/urls.py`
- Test: `guacozy_server/backend/test_agent.py`

**Interfaces:**
- Consumes: `users.models.User`, session-authenticated DRF (`IsAuthenticated`).
- Produces:
  - `AgentToken` fields: `id` UUID primary key, `user` FK, `name` CharField(max_length=60), `token_hash` CharField(max_length=64, unique=True), `created` auto_now_add, `last_used_at` null DateTime, `revoked_at` null DateTime.
  - `create_agent_token(user, name: str) -> tuple[AgentToken, str]` — second item is the raw secret, `secrets.token_urlsafe(32)`.
  - `hash_token(raw: str) -> str` — hex SHA-256.
  - `authenticate_bearer(authorization_header: str | None) -> User | None` — accepts only `Bearer <raw>`. Updates `last_used_at`. Returns `None` when missing, malformed, unknown, or `revoked_at` is set.
  - Session routes: `GET/POST /api/agent-tokens/`, `POST /api/agent-tokens/<uuid:token_id>/revoke/`.

- [ ] **Step 1: Write the failing test**

```python
def test_create_lists_hash_and_revoke(self):
    self.client.force_login(self.user)
    created = self.client.post("/api/agent-tokens/", {"name": "laptop"}, content_type="application/json")
    self.assertEqual(created.status_code, 201)
    secret = created.json()["token"]
    self.assertNotIn(secret, AgentToken.objects.get(name="laptop").token_hash)
    listed = self.client.get("/api/agent-tokens/").json()
    self.assertNotIn("token", listed[0])
    self.assertEqual(authenticate_bearer("Bearer " + secret).username, self.user.username)
    self.client.post(f"/api/agent-tokens/{listed[0]['id']}/revoke/")
    self.assertIsNone(authenticate_bearer("Bearer " + secret))
```

Also assert another user receives 404 on revoke, and `GET` after create does not return the secret.

- [ ] **Step 2: Run the test**

Run from `guacozy_server`: `../.venv/bin/python manage.py test backend.test_agent.AgentTokenTests -v 1`

Expected: FAIL because the route and model do not exist.

- [ ] **Step 3: Implement the model, `tokens.py`, and the session API**

Store only `hash_token(secret)`. Create response is `{"id", "name", "token", "created"}` with the raw `token`. List response is `{"id", "name", "created", "last_used_at", "revoked_at"}` and has no `token` key. Revoke sets `revoked_at` to now. Queryset is the requesting user's tokens only.

- [ ] **Step 4: Run the test**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guacozy_server/backend/models/agenttoken.py guacozy_server/backend/models/__init__.py \
  guacozy_server/backend/migrations/0017_agenttoken.py guacozy_server/backend/agent \
  guacozy_server/backend/api/views.py guacozy_server/backend/api/urls.py \
  guacozy_server/backend/test_agent.py
git commit -m "feat: add revocable agent bearer tokens"
```

---

### Task 2: MCP endpoint and whoami

**Files:**
- Create: `guacozy_server/backend/agent/errors.py`
- Create: `guacozy_server/backend/agent/mcp.py`
- Modify: `guacozy_server/guacozy_server/urls.py`
- Test: `guacozy_server/backend/test_agent.py`

**Interfaces:**
- Consumes: `authenticate_bearer`.
- Produces:
  - `ok(data: dict) -> dict` returns `{"ok": True, "data": data}`.
  - `fail(code: str, message: str, retry: str) -> dict` returns `{"ok": False, "error": {"code", "message", "retry"}}`.
  - `mcp_view(request) -> HttpResponse`. URL name `mcp`, path `mcp/`. `@csrf_exempt`. POST only, else 405.
  - `call_tool(user, name: str, arguments: dict) -> dict` returns an envelope. Task 2 registers only `guacozy_whoami`.
  - JSON-RPC methods: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`.
  - `initialize` echoes `protocolVersion` when the client sent `2024-11-05`, `2025-03-26`, or `2025-06-18`. Otherwise it returns `2025-03-26`. `serverInfo.name` is `guacozy`. `capabilities` is `{"tools": {}}`.
  - `tools/call` result content is `[{"type": "text", "text": <json.dumps(envelope)>}]`. `isError` is true when `ok` is false. HTTP status stays 200.
  - Missing or bad bearer: HTTP 401 and `fail("NOT_ALLOWED", "Authentication credentials were not provided.", "never")`.
  - `guacozy_whoami` data is `{"id": <pk>, "username": <username>, "is_staff": <bool>}`.

- [ ] **Step 1: Write the failing test**

```python
def test_mcp_ignores_session_cookie(self):
    self.client.force_login(self.user)
    response = self.client.post("/mcp/", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, content_type="application/json")
    self.assertEqual(response.status_code, 401)
    self.assertEqual(response.json()["error"]["retry"], "never")

def test_whoami_runs_as_the_token_user(self):
    body = self.rpc("tools/call", {"name": "guacozy_whoami", "arguments": {}})
    self.assertEqual(body["result"]["isError"], False)
    payload = json.loads(body["result"]["content"][0]["text"])
    self.assertEqual(payload["data"]["username"], self.user.username)
```

`self.rpc` sends `Authorization: Bearer <secret>` and a JSON-RPC 2.0 body. Also assert `initialize` returns `serverInfo.name == "guacozy"`.

- [ ] **Step 2: Run the test**

`../.venv/bin/python manage.py test backend.test_agent.AgentMcpTests -v 1`

Expected: FAIL with 404.

- [ ] **Step 3: Implement `errors.py` and `mcp_view`**

Dispatch on `method`. `notifications/initialized` returns HTTP 202 and an empty body. Unknown methods return JSON-RPC error `-32601` with HTTP 200. Unknown tool names return `fail("NOT_FOUND", ..., "never")` inside `tools/call`. Do not read `request.user` for authorization.

- [ ] **Step 4: Run the test**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guacozy_server/backend/agent/errors.py guacozy_server/backend/agent/mcp.py \
  guacozy_server/guacozy_server/urls.py guacozy_server/backend/test_agent.py
git commit -m "feat: serve MCP whoami over HTTP"
```

---

### Task 3: Folders and connections

**Files:**
- Create: `guacozy_server/backend/agent/management.py`
- Modify: `guacozy_server/backend/agent/mcp.py`
- Test: `guacozy_server/backend/test_agent.py`

**Interfaces:**
- Consumes: `ok`, `fail`, `call_tool`, `Folder`, `FolderPermission`, `ConnectionRdp`, `ConnectionVnc`, `ConnectionSsh`, `user_allowed_folders_ids`.
- Produces these tools, each returning an envelope. Arguments are JSON object keys:
  - `guacozy_list_folders()` → `{"folders": [{"id", "name", "parent"}]}` for folders the user may list.
  - `guacozy_create_folder(name, parent)` — `parent` is a folder id. Same allow check as `FolderFlatViewSet.perform_create`.
  - `guacozy_update_folder(folder_id, name, parent)` — same checks as `perform_update`. `name` and `parent` optional.
  - `guacozy_delete_folder(folder_id)` — same checks as `perform_destroy`, including non-empty refusal.
  - `guacozy_list_connections()` / `guacozy_get_connection(connection_id)` — connections whose `parent` is in `user_allowed_folders_ids(user, require_view_permission=True)`.
  - `guacozy_create_connection(protocol, name, host, parent, ...)` — staff only. `protocol` is `rdp`, `vnc`, or `ssh`.
  - `guacozy_update_connection(connection_id, ...)` — staff only, and the connection must be visible.
  - `guacozy_delete_connection(connection_id)` — staff only, and the connection must be visible.
- Connection `data` keys: `id`, `name`, `protocol`, `parent`, `host`, `port`, `username`, `domain`, `passthrough_credentials`, `has_password`, `has_private_key`, `guacdserver`, plus the non-secret fields of that child model. Never `password`, `private_key`, or `passphrase`.
- Allowed extra fields, rejected otherwise with `INVALID_ARGS` / `never`:
  - RDP: `ignore_cert`, `security`, `disable_auth`, `console`, `initial_program`, `server_layout`, `color_depth`, `resize_method`
  - VNC: `autoretry`, `color_depth`, `swap_red_blue`, `cursor_remote`, `read_only`, `repeater_dest_host`, `repeater_dest_port`, `reverse_connect`, `listen_timeout`, `clipboard_encoding`
  - SSH: `host_key`, `server_alive_interval`, `private_key`, `passphrase`, `font_size`, `command`
  - Shared optional: `port`, `username`, `password`, `domain`, `passthrough_credentials`, `guacdserver`
- Blank `password` on update does not clear the stored password. Sending `password` with a non-blank value replaces it.
- Do not add a staff bypass that creates a root folder or lists every connection. A missing `parent` on create is `INVALID_ARGS` / `never`. The caller needs a folder they can already view, which tests create with `FolderPermission`.

- [ ] **Step 1: Write the failing test**

```python
def test_connection_payload_omits_secrets(self):
    payload = self.tool("guacozy_create_connection", {
        "protocol": "rdp", "name": "WINDOWS10", "host": "10.0.0.5",
        "parent": self.folder.id, "password": "s3cret", "private_key": "nope",
    })
    self.assertTrue(payload["ok"])
    self.assertNotIn("password", payload["data"])
    self.assertNotIn("private_key", payload["data"])
    self.assertNotIn("passphrase", payload["data"])
    self.assertTrue(payload["data"]["has_password"])
    self.assertEqual(ConnectionRdp.objects.get(name="WINDOWS10").password, "s3cret")

def test_non_staff_cannot_create_connection(self):
    payload = self.tool_as(self.member, "guacozy_create_connection", {
        "protocol": "vnc", "name": "screen", "host": "10.0.0.6", "parent": self.folder.id,
    })
    self.assertEqual(payload["error"]["code"], "NOT_ALLOWED")
    self.assertEqual(payload["error"]["retry"], "never")

def test_hidden_connection_is_not_found(self):
    payload = self.tool_as(self.outsider, "guacozy_get_connection", {"connection_id": str(self.visible.id)})
    self.assertEqual(payload["error"]["code"], "NOT_FOUND")

def test_blank_password_keeps_the_old_one(self):
    self.tool("guacozy_update_connection", {"connection_id": str(self.visible.id), "password": ""})
    self.assertEqual(ConnectionRdp.objects.get(pk=self.visible.id).password, "s3cret")

def test_unknown_field_is_invalid(self):
    payload = self.tool("guacozy_create_connection", {
        "protocol": "ssh", "name": "shell", "host": "10.0.0.7",
        "parent": self.folder.id, "nope": 1,
    })
    self.assertEqual(payload["error"]["code"], "INVALID_ARGS")

def test_delete_non_empty_folder_is_refused(self):
    payload = self.tool("guacozy_delete_folder", {"folder_id": self.folder.id})
    self.assertEqual(payload["error"]["code"], "NOT_ALLOWED")
```

Fixture: staff user with `FolderPermission` on `self.folder`, a member who can view that folder but `is_staff=False`, and an outsider with no permission. `self.tool` calls `tools/call` as the staff token.

Also assert a create log capture does not contain `s3cret` (`assertLogs`).

- [ ] **Step 2: Run the test**

`../.venv/bin/python manage.py test backend.test_agent.AgentManagementTests -v 1`

Expected: FAIL because the tools are unknown (`NOT_FOUND`).

- [ ] **Step 3: Implement `management.py` and register the eight tools**

Build the child model from `protocol` on create. On update, ignore a blank `password` key so `Connection.save` is not the only thing protecting it: do not assign `""`. Log the tool name and the connection id, not the argument dict.

- [ ] **Step 4: Run the test**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guacozy_server/backend/agent/management.py guacozy_server/backend/agent/mcp.py \
  guacozy_server/backend/test_agent.py
git commit -m "feat: let an agent manage folders and connections"
```

---

### Task 4: Folder access and tickets

**Files:**
- Modify: `guacozy_server/backend/agent/management.py`
- Modify: `guacozy_server/backend/agent/mcp.py`
- Test: `guacozy_server/backend/test_agent.py`

**Interfaces:**
- Consumes: Task 3 tools and `Ticket`.
- Produces:
  - `guacozy_set_folder_access(folder_id, action, user_id=None, group_id=None)` — `action` is `grant` or `revoke`. Staff only. Exactly one of `user_id` and `group_id`. Writes or deletes one `FolderPermission`. Any existing folder id is addressable by staff, because this is the admin grant, not the view list.
  - `guacozy_list_tickets()` — tickets the user authors or holds, same filter as `TicketViewSet.get_queryset` without deleting other users' expired tickets.
  - `guacozy_create_ticket(connection_id, validity_seconds=86400)` — connection must be visible. `author` and `user` are the caller. `control` stays True. `parent` stays null.
  - `guacozy_delete_ticket(ticket_id)` — only a ticket the caller authors or holds. Otherwise `NOT_FOUND`.
- Ticket `data` keys: `id`, `connection_id`, `control`, `parent`, `validto` ISO 8601.

- [ ] **Step 1: Write the failing test**

```python
def test_staff_can_grant_folder_access(self):
    payload = self.tool("guacozy_set_folder_access", {
        "folder_id": self.folder.id, "action": "grant", "user_id": self.member.id,
    })
    self.assertTrue(payload["ok"])
    self.assertTrue(self.folder.users.filter(pk=self.member.id).exists())

def test_member_ticket_is_theirs(self):
    payload = self.tool_as(self.member, "guacozy_create_ticket", {"connection_id": str(self.visible.id)})
    self.assertEqual(payload["data"]["connection_id"], str(self.visible.id))
    ticket = Ticket.objects.get(pk=payload["data"]["id"])
    self.assertEqual(ticket.user, self.member)
    self.assertEqual(ticket.author, self.member)
```

Also assert a non-staff `set_folder_access` is `NOT_ALLOWED` / `never`, and both `user_id` and `group_id` is `INVALID_ARGS`.

- [ ] **Step 2: Run the test**

`../.venv/bin/python manage.py test backend.test_agent.AgentTicketTests -v 1`

Expected: FAIL with `NOT_FOUND` for the unknown tool.

- [ ] **Step 3: Register the four tools**

Do not delete expired tickets globally from these tools.

- [ ] **Step 4: Run the test**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guacozy_server/backend/agent/management.py guacozy_server/backend/agent/mcp.py \
  guacozy_server/backend/test_agent.py
git commit -m "feat: let an agent manage tickets and folder access"
```

---

### Task 5: Start panel token control

**Files:**
- Modify: `frontend/src/Api/GuacozyApi.js`
- Modify: `frontend/src/Context/AppContext.jsx`
- Modify: `frontend/src/Components/Welcome/Welcome.jsx`
- Modify: `guacozy_server/static/css/styles.css`
- Test: `guacozy_server/backend/test_agent.py` (API already covered) and the production bundle.

**Interfaces:**
- Consumes: `GET/POST /api/agent-tokens/`, `POST /api/agent-tokens/<id>/revoke/`, axios instance with CSRF.
- Produces: on the start panel, inside a `section.gc-panel`, a kicker `Agent token`, a name field, a button labeled `Create token`, a one-time secret element `.gc-token-secret`, and a `Revoke` button per token name. Actions `createAgentToken(name)`, `revokeAgentToken(id)`, and `loadAgentTokens()` live on `appState.actions`. The secret is React state and is cleared on revoke and on the next create. It is not written to `localStorage`.

- [ ] **Step 1: Implement the panel**

Reuse `.gc-panel`, `.gc-kicker`, and `.gc-logout` for the buttons. New rules may only set layout for `.gc-token-row` and `.gc-token-secret` using existing `--gc-*` variables. No new colors.

- [ ] **Step 2: Build the frontend**

From `frontend`: `npm run build`

Expected: exit 0.

- [ ] **Step 3: Check the panel in a browser**

Sign in, open the start tab, create a token, confirm the secret is visible, reload, confirm the secret is gone and the name remains, revoke it, confirm the name is gone. If no browser is available, say so and leave this step unchecked.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/Api/GuacozyApi.js frontend/src/Context/AppContext.jsx \
  frontend/src/Components/Welcome/Welcome.jsx guacozy_server/static/css/styles.css
git commit -m "feat: create and revoke agent tokens from the start panel"
```

The production bundle under `frontend/build/` is generated by the image build. Do not commit it unless it is already tracked and this repo's practice is to commit it. Check `git status` and follow the existing ignore rule.

---

### Task 6: Frame compositor

**Files:**
- Create: `guacozy_server/backend/agent/frame.py`
- Modify: `guacozy_server/requirements-base.txt`
- Test: `guacozy_server/backend/test_agent.py`

**Interfaces:**
- Consumes: Pillow.
- Produces:
  - `class Frame` with `apply(instruction) -> None`, `png_bytes() -> bytes`, `cursor -> tuple[int, int]`, `size -> tuple[int, int]`.
  - `instruction` is `guacamole.instruction.GuacamoleInstruction`.
  - Default layer is `0`. `size` sets that layer's width and height. A new layer starts transparent.
  - `img` begins a stream (`stream`, `mode`, `layer`, `mime`, `x`, `y`). Following `blob` chunks are base64. `end` pastes the decoded `image/png` at `x, y` on that layer. Other mimes raise `ValueError`.
  - `rect` then `cfill` fills that rectangle on the layer with the given RGBA.
  - `copy` copies a rectangle from one layer onto another.
  - `cursor` records the pointer `x, y`. It does not draw the pointer into the PNG.
  - `png_bytes()` encodes layer `0`. Origin is top left.

- [ ] **Step 1: Write the failing test**

```python
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
    frame.apply(GuacamoleInstruction("copy", "0", "0", "1", "0", "1", "1", "0", "1"))
    frame.apply(GuacamoleInstruction("cursor", "4", "5", "0", "0", "0", "0", "0"))
    image = Image.open(io.BytesIO(frame.png_bytes()))
    self.assertEqual(image.getpixel((0, 1))[:3], (255, 0, 0))
    self.assertEqual(frame.cursor, (4, 5))
```

`copy` args are destination layer, source layer, source x, source y, source width, source height, destination x, destination y. The call above copies the red pixel at (1, 0) to (0, 1) on layer 0. `cursor` args are x, y, then the source rectangle, which is ignored for the PNG. `rect` then `cfill` use layer, x, y, width, height and layer, red, green, blue, alpha.

Build `RED_PNG_B64` with Pillow in the test. Add Pillow to `requirements-base.txt` pinned to the version `../.venv/bin/pip install Pillow` resolves, and install it before the test run.

- [ ] **Step 2: Run the test**

`../.venv/bin/python manage.py test backend.test_agent.FrameTests -v 1`

Expected: FAIL on import of `Frame`.

- [ ] **Step 3: Implement `Frame`**

Ignore opcodes other than `size`, `img`, `blob`, `end`, `rect`, `cfill`, `copy`, and `cursor`.

- [ ] **Step 4: Run the test**

Same command. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add guacozy_server/backend/agent/frame.py guacozy_server/requirements-base.txt \
  guacozy_server/backend/test_agent.py
git commit -m "feat: composite a Guacamole frame to PNG"
```

---

### Task 7: Shared desktop session

**Files:**
- Create: `guacozy_server/backend/agent/desktop.py`
- Modify: `guacozy_server/backend/agent/mcp.py`
- Test: `guacozy_server/backend/test_agent.py`

**Interfaces:**
- Consumes: `TunnelClient`, `Frame`, `ok`, `fail`, `Ticket`, `Connection.get_guacamole_parameters`, `get_ticket_sessionid` behavior (parent session id wins).
- Produces tools:
  - `guacozy_open_desktop(connection_id=None, ticket_id=None)` — exactly one argument. Visible connection, or a ticket the user authors or holds. Creates a ticket when given a connection. Joins `$` + session id when the ticket or its parent already has one. Otherwise handshakes a new session and stores the id on the parent ticket the way `GuacamoleConsumer.update_ticket_sessionid` does, so the existing browser tunnel joins that same id. Data: `{"session_id", "ticket_id", "width", "height", "joined"}`. `joined` is true when it attached to an existing id. A new session returns width 1280 and height 720. A join returns the frame size at return time, which may be 0 until the server sends `size`. `guacozy_snapshot` is the size the agent clicks against.
  - `guacozy_snapshot(session_id)` — data `{"session_id", "width", "height", "png_base64", "cursor": {"x", "y"}, "note": "Pixels, origin top left. There are no element refs."}`.
  - `guacozy_click(session_id, x, y, button="left", count=1)` — `button` is `left`, `right`, or `middle` (masks 1, 4, 2). Each count sends mouse down then up.
  - `guacozy_type(session_id, text)` — key down and up per character.
  - `guacozy_press(session_id, key)` — key down then key up. Keys: letters, digits, `Enter` 0xff0d, `Tab` 0xff09, `Backspace` 0xff08, `Escape` 0xff1b, `Left` 0xff51, `Up` 0xff52, `Right` 0xff53, `Down` 0xff54, `Shift` 0xffe1, `Control` 0xffe3, `Alt` 0xffe9.
  - `guacozy_scroll(session_id, x, y, delta)` — positive `delta` uses button mask 8 (up). Negative `delta` uses mask 16 (down). One click per unit of `delta`, capped at 5.
  - `guacozy_clipboard_get(session_id)` data is `{"text": "<current clipboard>"}`. `guacozy_clipboard_set(session_id, text)` sends the clipboard instruction and stores that text for the next get.
  - `guacozy_close_desktop(session_id)` — closes only this agent socket.
- A session id unknown to this process, or owned by another user, is `NOT_FOUND` / `never`.
- Dead guacd: snapshot returns `STALE_SESSION` / `safe`. Click, type, press, scroll, and clipboard return `STALE_SESSION` / `unsafe`.
- `REMOTE_DESKTOP` / `unsafe` is for a handshake or protocol failure while opening.
- `AgentClient(TunnelClient)` overrides `handshake`. New session: `size` 1280, 720, 96, then `audio` empty, `video` empty, `image` `image/png`, then `connect`. Join (`connectionid` set): same sequence with no `size` instruction. Then `relax_read_timeout()`.
- A background thread receives. `sync` is answered with `sync` and the same args before the frame is published. Send from the tool thread. One lock per session around send. Do not log instruction bodies.

- [ ] **Step 1: Write the failing test**

Use a fake guacd on `127.0.0.1:0`, same idea as `TicketTunnelConnectTests`: one instruction in, `4.args` out (no required connect args), then expect the client handshake, then `5.ready,$<uuid>;`. For the open-new test, record that a `size` instruction with args `1280`, `720`, `96` was sent, then send a 1×1 PNG `img`/`blob`/`end` and `sync` timestamp `1`. Snapshot pixels are red at the pasted coordinate, and the fake received `sync` with timestamp `1`.

```python
def test_join_does_not_send_size(self):
    self.ticket.sessionid = uuid.uuid4()
    self.ticket.save()
    payload = self.tool("guacozy_open_desktop", {"ticket_id": str(self.ticket.id)})
    self.assertTrue(payload["data"]["joined"])
    self.assertNotIn("size", self.fake.opcodes)

def test_press_releases_key(self):
    self.tool("guacozy_press", {"session_id": self.session_id, "key": "Shift"})
    keys = [args for opcode, args in self.fake.sent if opcode == "key"]
    self.assertEqual(keys, [["65505", "1"], ["65505", "0"]])

def test_click_survives_a_quiet_socket(self):
    # receive is blocked in the poller; click must still arrive within 2s
    self.tool("guacozy_click", {"session_id": self.session_id, "x": 10, "y": 20, "button": "left"})
    self.assertTrue(self.fake.mouse_seen.wait(2))

def test_click_failure_retry_is_unsafe(self):
    self.fake.stop()
    payload = self.tool("guacozy_click", {"session_id": self.session_id, "x": 1, "y": 1})
    self.assertEqual(payload["error"]["code"], "STALE_SESSION")
    self.assertEqual(payload["error"]["retry"], "unsafe")

def test_close_leaves_the_other_join_open(self):
    # second fake client joined with the same connection id and is still reading
    self.tool("guacozy_close_desktop", {"session_id": self.session_id})
    self.assertTrue(self.other_join.alive)
```

Also assert snapshot `note` is exactly `Pixels, origin top left. There are no element refs.` Assert clipboard text `clipboard-secret` does not appear in `assertLogs`. Assert a second user's token gets `NOT_FOUND` for the first user's `session_id`. Patch `GuacamoleClient.__init__` timeout the way `test_input_is_forwarded_while_guacd_is_quiet` does, so a shared thread would fail the quiet-socket test.

- [ ] **Step 2: Run the test**

`../.venv/bin/python manage.py test backend.test_agent.AgentDesktopTests -v 1`

Expected: FAIL on unknown tool or missing `AgentClient`.

- [ ] **Step 3: Implement `desktop.py` and register the nine tools**

Store sessions in a module dict guarded by a lock. `open_desktop` on a new connection uses `get_guacamole_parameters(user)` for the connect args. Set `scope`-less ticket log actions through `TicketLog.addlog` only when an IP is not required; if `addlog` needs a request, pass the MCP request's IP. Do not close the guacd session id when the agent socket closes. Disconnect of this client calls `TunnelClient.close` only.

- [ ] **Step 4: Run the test**

Same command, then the full suite:

`../.venv/bin/python manage.py test -v 1`

Expected: PASS, including the existing tunnel tests.

- [ ] **Step 5: Commit**

```bash
git add guacozy_server/backend/agent/desktop.py guacozy_server/backend/agent/mcp.py \
  guacozy_server/backend/test_agent.py
git commit -m "feat: drive a shared desktop from agent tools"
```

---

### Task 8: Agent guide

**Files:**
- Modify: `AGENTS.md`
- Test: `guacozy_server/backend/test_agent.py`

**Interfaces:**
- Consumes: the finished tool names and `/mcp/`.
- Produces: a section in `AGENTS.md` that tells an agent to create a token on the start panel, send `Authorization: Bearer` to `POST /mcp/`, call `guacozy_snapshot` before `guacozy_click`, `guacozy_type`, or `guacozy_press`, use pixels from that snapshot, and call `guacozy_open_desktop` again after `STALE_SESSION`.

- [ ] **Step 1: Write the failing test**

```python
def test_agents_md_states_the_loop(self):
    text = pathlib.Path(__file__).resolve().parents[2].joinpath("AGENTS.md").read_text()
    self.assertIn("/mcp/", text)
    self.assertIn("Authorization: Bearer", text)
    self.assertIn("guacozy_snapshot", text)
    self.assertIn("STALE_SESSION", text)
    self.assertIn("pixel", text.lower())
```

- [ ] **Step 2: Run the test**

`../.venv/bin/python manage.py test backend.test_agent.AgentGuideTests -v 1`

Expected: FAIL because the section is absent.

- [ ] **Step 3: Add the section**

Keep the five existing commands. Do not add a MkDocs page.

- [ ] **Step 4: Run the full suite**

`../.venv/bin/python manage.py test -v 1`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md guacozy_server/backend/test_agent.py
git commit -m "docs: tell agents how to use Guacozy MCP"
```
