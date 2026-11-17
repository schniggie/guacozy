# Agent use of Guacozy

## Intent

The person already talks to an outside agent. That agent should be able to do both jobs a person does in Guacozy: drive a remote desktop, and manage Guacozy itself.

They asked for this on 2026-11-16, after remote desktops were usable again, and pointed at Vercel’s agent-desktop as the computer-use idea to borrow. The idea that transfers is the loop: look, act, look again, with the model staying in the agent. What does not transfer is the accessibility tree. A Guacamole session carries a picture and keyboard and mouse, not the remote operating system’s buttons.

## What they decided

- The agent interacts with every remote desktop Guacozy can open. They named RDP and VDP. VDP is read as VNC. SSH is included: it is already a connection type, and the same picture-and-keyboard loop covers a terminal.
- The same agent adds connections, changes existing ones, and does the rest of that bookkeeping. Folders, tickets, and who can reach a folder were the guessed meaning of “and so on,” and they accepted that picture.
- The model stays in the agent they already use. Guacozy exposes tools. It does not grow a chat box.
- Success looks like this: they ask for a new Windows connection, then ask the agent to log into it and do something they can watch.
- The agent signs in as that person. Audit entries show their user.

## Calls made here

These were stated while shaping the work and were not objected to, or they are settled here so a builder does not have to guess.

- The tool surface is an MCP server inside the Django app, reached over HTTP on the existing Guacozy origin. The agent is not inside the container, so a stdio-only server is the wrong primary interface. A stdio wrapper is out of scope.
- The agent proves who it is with a personal bearer token created while the person is logged in. The password is never stored in the agent’s config. The token is shown once, stored only as a hash, and can be revoked. A revoked or missing token is rejected.
- The agent can only do what that user can already do. Folder view rules still hide connections. Creating, editing, and deleting connections, and changing who can reach a folder, stay staff actions, because that work is admin-only today. Opening a desktop and creating a ticket stay available to a user who can already see that connection.
- Watching uses the ticket session Guacozy already has. One guacd session, two clients. The person’s browser keeps the sidebar ticket. The agent holds its own protocol client joined to that same session id. The person sees the pointer move. The agent does not scrape the browser canvas, and it does not need the browser tab to be open in order to see or act.
- The look step returns a PNG of the current frame, in pixels, origin at the top left. There are no element refs. A click uses those pixels. This is the deliberate difference from agent-desktop.
- The agent never sends a display size when it joins a session that already exists. Ticket sharing already says only the original client updates resolution, so two clients do not fight over the window. When the agent is the first client, it sets 1280×720 once.
- The agent client must answer Guacamole sync instructions. A client that only receives and never syncs is the failure that already killed remote input.
- Press sends a key down and then up. There is no tool that holds a key or mouse button down, for the same reason agent-desktop refuses those commands until something can release them.
- Passwords, private keys, clipboard contents, and screenshots are never written to logs.
- Sessions live in the one Daphne process Guacozy already runs. They are not shared across processes.
- `AGENTS.md` gains a short section that tells an agent how to reach the MCP server, how to get a token, and the look-act-look rules. The existing MkDocs book is not rewritten.

## Goals

- An outside agent, signed in as the user, can list, create, change, and delete folders and connections the user is allowed to manage, and can change folder access when the user is staff.
- The same agent can open an RDP, VNC, or SSH connection, receive a PNG of the screen, and send click, type, press, scroll, and clipboard actions through that session.
- The person can open the same ticket in the current UI and watch those actions.
- A failed tool call returns a stable JSON error the agent can retry only when the error says retry is safe.

## Non-goals

- A chat panel inside Guacozy.
- Accessibility trees, element refs, or installing agent-desktop on the remote machine.
- OCR or pixel-template matching inside Guacozy. The agent reads the picture.
- A new remote protocol, a second guacd, or a headless browser.
- Changing folder permission rules, the visual system, login, or the tunnel timeout fix.
- Rewriting the MkDocs installation book.
- Bumping dependency pins that this work does not need.
- Multiple Daphne workers, or a session that survives a process restart.

## Anti-goals

- A token or tool that can see or change another user’s connections or desktops.
- A snapshot tool that works only by reading the pixels of an open browser tab.
- A write API that returns stored passwords or private keys.
- An input tool that can leave a modifier key stuck down.

## How the pieces fit

```
agent  --bearer token-->  MCP HTTP on Guacozy
                              |-- management tools --> existing models and rules
                              |-- desktop tools -----> agent protocol client
                                                          |
person's browser --ticket websocket---------------------- guacd session
```

The person’s browser and the agent are two clients of one guacd session. Management tools do not open that session.

### Sign-in

Add a token the logged-in user can create and revoke from the shell they already use. Show the secret once. Store a hash. Each MCP request with `Authorization: Bearer …` runs as that user. Session cookies are not accepted on the MCP path, so this path does not depend on the CSRF behavior of the browser API.

### Management tools

Use the models that already exist: `Folder`, `FolderPermission`, `Connection` and its RDP, VNC, and SSH children, and `Ticket`.

The connection REST view is read-only today. Creating and editing a connection is new, and it goes through these tools rather than turning the public connection API into a surprise write API. Folder create, update, and delete already exist on the folder API and keep their current checks, including the refusal to delete a folder that still has children or connections. Ticket create already exists and assigns the ticket to the requesting user.

Connection responses omit `password`, `private_key`, and `passphrase`. A blank password on update keeps the stored password, which the model already does. Protocol-specific fields are the fields already on `ConnectionRdp`, `ConnectionVnc`, and `ConnectionSsh`. The tool names the protocol on create. Unknown fields are rejected.

Folder access is a staff tool: grant or revoke one user or one group on one folder. It writes `FolderPermission`. It does not invent per-connection permissions.

Suggested tool names, so the plan has one list:

- `guacozy_whoami`
- `guacozy_list_folders`, `guacozy_create_folder`, `guacozy_update_folder`, `guacozy_delete_folder`
- `guacozy_list_connections`, `guacozy_get_connection`, `guacozy_create_connection`, `guacozy_update_connection`, `guacozy_delete_connection`
- `guacozy_set_folder_access`
- `guacozy_list_tickets`, `guacozy_create_ticket`, `guacozy_delete_ticket`

### Desktop tools

`guacozy_open_desktop` takes a connection the user can see, or an existing ticket they own. It creates a ticket if needed, then connects a server-side Guacamole client.

If that ticket, or its parent, already has a guacd session id, the client joins `$` plus that id. Otherwise it handshakes a new session with the connection’s existing `get_guacamole_parameters`, stores the session id on the parent ticket the way `GuacamoleConsumer` already does, and sends one size of 1280×720.

The ticket remains visible in the current sidebar. When the person opens it, the existing websocket consumer joins the same session id. The agent’s socket and the browser’s socket are independent. Disconnecting one must not close the other.

`guacozy_snapshot` returns the current frame after the client has acknowledged sync. The frame is a PNG built from the Guacamole drawing instructions for the default layer: size, image streams (`img`, `blob`, `end`) in the mime types guacd sends, and the copy and fill instructions needed so a later snapshot is not a hole where an earlier image used to be. Cursor position is reported beside the image so the agent can see its own pointer. Building a full remote-desktop codec is not required.

`guacozy_click`, `guacozy_type`, `guacozy_press`, and `guacozy_scroll` send Guacamole mouse and key instructions on that client. `type` turns text into keysyms. Coordinates are pixels of the latest snapshot.

`guacozy_clipboard_get` and `guacozy_clipboard_set` use the session clipboard. `guacozy_close_desktop` closes only the agent’s client and leaves the person’s ticket in place.

If the guacd session is gone, the tool returns `STALE_SESSION` and the agent must open again. Retry is `safe` only when the call did not change the remote machine. Clicks, typing, and clipboard writes are `unsafe` to retry blindly. Permission and missing-object errors are `never`.

There is no `STALE_REF` code. There are no refs.

### Errors

Every tool returns one JSON object:

```json
{ "ok": true, "data": {} }
```

```json
{
  "ok": false,
  "error": {
    "code": "NOT_ALLOWED",
    "message": "You are not allowed to change this connection.",
    "retry": "never"
  }
}
```

Codes: `INVALID_ARGS`, `NOT_ALLOWED`, `NOT_FOUND`, `STALE_SESSION`, `REMOTE_DESKTOP`.

### Tests

Django tests, run from `guacozy_server` as `AGENTS.md` already says.

- A valid token acts as its user. A missing, wrong, or revoked token is rejected.
- A staff user can create an RDP connection and list it. A non-staff user cannot create one. A fetched connection does not contain the password.
- A user without folder access cannot list or open that connection.
- Against a fake guacd, the same shape as the existing tunnel test: the agent client survives a quiet period, a snapshot returns a PNG that was sent as an image instruction, and a click sends a mouse instruction while receive is blocked.
- A second join, using the stored session id, receives the session. Closing the agent client does not close that second join.

### User-facing token control

On the start panel, a person who is logged in can create a token, copy it once, see the names of tokens they already created, and revoke one. Use the existing `styles.css` tokens. No new visual language.

## Left to the builder

- The Python MCP library, and the exact ASGI mount, as long as the result is HTTP MCP on the Guacozy origin and nginx proxies it with the same forwarded headers as the rest of the app. If a library cannot sit on Django 6, a small MCP streamable-HTTP handler in Django is acceptable. The tool list and the error shape above are the contract.
- The exact keysym map for `type` and `press`, as long as letters, digits, Enter, Tab, Backspace, Escape, arrows, and the common modifiers work.
- How the in-memory session table is locked, as long as one desktop session can receive and send at the same time and two desktops do not share a socket.
- The wording of the `AGENTS.md` section, as long as it tells the agent to snapshot before acting, to use pixels from that snapshot, and to open again after `STALE_SESSION`.

## When this design is wrong

- If the remote desktop must be driven by named controls rather than pixels, this design is the wrong shape. That would require software on the remote machine, which this work does not add.
- If more than one Daphne process is introduced, in-memory desktop sessions stop being valid.
- If the person wants the agent to sign in as a different Guacozy user, the token and the audit story change.
