# Guacozy

Guacozy is an HTML5 VNC, RDP, and SSH connection manager built on [Apache Guacamole](https://guacamole.apache.org/). Django keeps the accounts, folders, connections, tickets, and audit log. The browser UI is a React shell.

An outside agent can sign in as you. It manages folders, connections, tickets, and folder access, and it drives the same remote desktop you are watching. The model stays outside Guacozy. Guacozy exposes the tools over HTTP.

## Links

Source for this tree: [github.com/schniggie/guacozy](https://github.com/schniggie/guacozy)

Longer guides live in [`docs/`](docs/index.md). The site [guacozy.readthedocs.io](https://guacozy.readthedocs.io) still describes the original project. Where that site disagrees with this file, follow this file.

The image [`guacozy/guacozy-server`](https://hub.docker.com/r/guacozy/guacozy-server) on Docker Hub is that older release. Build this tree locally.

Video demo:

[![Guacozy video demo](https://img.youtube.com/vi/R5uCPrH9mnw/0.jpg)](https://www.youtube.com/watch?v=R5uCPrH9mnw)

Screenshot:

![Guacozy screenshot](docs/img/guacozy-screenshot-1.jpg "Guacozy screenshot")

## Run

You need Docker Compose v2. The default compose file starts Guacozy, guacd, and Postgres.

### Default stack

From the repo root:

```shell
docker compose up --build
```

That builds the production image and starts:

- `server` — nginx, Daphne, and the built React app
- `guacd` — `guacamole/guacd:1.6.0`
- `db` — Postgres 18

Copy `.env.example` to `.env` and set `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, and `POSTGRES_PASSWORD` before the first start. Compose refuses to start while any of those are empty. `POSTGRES_PASSWORD` must be letters and digits so it can sit in `DJANGO_DB_URL`.

The published port is `10443` (HTTPS). Open:

```text
https://localhost:10443/cozy/
```

Port 80 stays inside the container and is not published. A self-signed certificate is written to the `guacozy-ssl` volume when `/ssl` is empty, and later starts keep that pair. The browser warns until you replace it.

A superuser is created only when `SUPERUSER_PASSWORD` is set. The username defaults to `admin` and the email to `admin@example.com`. Leave the password unset when you do not want that account. Deleting the user does not bring it back unless the password is still set.

`docker-compose-qa.yml` is the same stack, including the `postgres18-data` volume.

After you change a Dockerfile, the Python requirements, the frontend, or `guacozy_server/static/css/styles.css`, run `docker compose up --build` again. A running container keeps the image it was built from.

### Development stack

```shell
docker compose -f docker-compose.dev.yml up --build
```

- Django `runserver` on port `8000`, with `./guacozy_server` mounted at `/app` and `DEBUG=true`
- Vite on port `3000`, with `./frontend` mounted
- The shared stylesheet mounted read-only so the dev server and the login page use one file
- Postgres 18 on its own volume, `dev-postgres18-data`
- `guacamole/guacd:1.6.0`

It uses the same repo-root `.env` as the default stack and does not publish TLS.

Open `http://localhost:3000/`. The dev server proxies `/api`, `/admin`, `/accounts`, `/staticfiles`, and the `/tunnelws` websocket to Django.

### Postgres 18

Postgres 18 stores the cluster in `/var/lib/postgresql/18/docker`. The compose files mount a new volume at `/var/lib/postgresql`:

| Stack | Volume |
| --- | --- |
| `docker compose up` and `docker-compose-qa.yml` | `postgres18-data` |
| `docker-compose.dev.yml` | `dev-postgres18-data` |

The older volumes `postgres-data` and `dev-postgres-data` stay on disk. They are a Postgres 10 cluster (`PG_VERSION` at the volume root). Leave them unmounted. Mounting one at `/var/lib/postgresql` makes Postgres 18 exit. This stack starts a new cluster. It does not copy the old database.

`DJANGO_DB_URL` is `postgres://postgres:<POSTGRES_PASSWORD>@db:5432/postgres`. Postgres requires that password on a new volume.

A volume created by an older compose file used trust authentication. Postgres applies `POSTGRES_PASSWORD` only when the volume is first initialized, so that existing volume keeps trusting connections. The password in `.env` is what a new volume will use. To make the old volume require it, set the role password and `pg_hba`, or start a new volume.

### A guacd you already run

The compose files already include `guacamole/guacd:1.6.0`. Do not use the `latest` tag. To use another guacd, set its hostname and port (usually `4822`) on the Guacozy guacd server record. Use an address the Guacozy container can route to. `127.0.0.1` inside that container is the container itself.

The browser client stays on `guacamole-common-js` 1.5.0, the last package Apache published. guacd 1.6 still speaks that protocol. The Python tunnel stays on `pyguacamole` 0.11, the last release on PyPI.

## Environment variables

Compose reads a gitignored `.env` in the repo root. `guacozy_server/.env` is the file `manage.py` reads when you run Django on the host. Copy the matching `.env.example` and fill it in. Nothing in git supplies these values.

| Variable | Purpose |
| --- | --- |
| `DEBUG` | Django debug mode. Dev compose sets it true. Production leaves it unset, so debug is off. |
| `DJANGO_ALLOWED_HOSTS` | Host header allow list. Defaults to `localhost,127.0.0.1`. Add every hostname you open. See the [Django setting](https://docs.djangoproject.com/en/6.1/ref/settings/#allowed-hosts). |
| `DJANGO_SECRET_KEY` | Signing key. Use a long random string. Required. |
| `FIELD_ENCRYPTION_KEY` | Encrypts connection passwords. Required. Keep the key the database was written with. |
| `POSTGRES_PASSWORD` | Database password. Letters and digits only. Required by Compose. |
| `DJANGO_TIME_ZONE` | Time zone, for example `Europe/Vilnius` |
| `DJANGO_DB_URL` | Database URL. Compose sets this from `POSTGRES_PASSWORD`. See [django-environ](https://django-environ.readthedocs.io/en/latest/). |
| `SUPERUSER_NAME`, `SUPERUSER_EMAIL`, `SUPERUSER_PASSWORD` | When the password is set, startup creates that user if the username is missing. |
| `CSRF_TRUSTED_ORIGINS` | Optional extra origins, including the scheme and port. |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | Optional overrides. Default is on when `DEBUG` is off. |

Generate the three required values before the first start:

```shell
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(50))"
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
.venv/bin/python -c "import secrets; print(secrets.token_hex(24))"
```

Those lines are `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, and `POSTGRES_PASSWORD`.

Changing `FIELD_ENCRYPTION_KEY` later leaves passwords already stored under the old key unreadable. The process exits if `DJANGO_SECRET_KEY` or `FIELD_ENCRYPTION_KEY` is empty. It does not invent a replacement.

`manage.py test` uses a test-only key when those variables are unset. `manage.py check` does not.

## SSL

Nginx listens on port 443. Port 80 inside the container redirects to `https://$host`. That listener is not published. Open `https://<host>:10443/`. Publishing port 80 and following the redirect would send the browser to port 443, which is not this service when you mapped 10443.

Responses on 443 send HSTS (`max-age=31536000`, this host only). A browser that has seen it upgrades later plain `http://` requests for that hostname to `https://` on port 443.

`SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` follow `DEBUG`: on when debug is off. The CSRF cookie stays readable by JavaScript because the API client sends `csrftoken`. Dev compose sets `DEBUG=true` so `http://localhost:3000` can keep the session.

The `guacozy-ssl` volume is mounted at `/ssl`. A self-signed pair is created only when both files are missing. An existing pair is left as it is. Nginx reads:

```text
/ssl/cert.crt
/ssl/cert.key
```

`cert.crt` is the server certificate followed by the CA chain:

```shell
cat certificate.crt ca.crt > cert.crt
```

Mount your own directory when you want a different certificate:

```yaml
services:
  server:
    volumes:
      - ./myssldir:/ssl
```

## What this tree includes

- Python 3.13, Django 6.1, Channels 4, Daphne 4, Django REST framework 3.18, and Pillow 13 (PNG frames for the agent).
- React 19 and Vite 8, built with Node 26. Create React App is gone. The production build is `frontend/build/`, served at `/cozy/`.
- axios 1.21.1 for `/api/`. That is the current release. The client is `frontend/src/Api/GuacozyApi.js`.
- The logged-in shell does not use semantic-ui-react. Login still uses the Django semantic form styles.
- One stylesheet, `guacozy_server/static/css/styles.css`, for the login page and the logged-in shell.
- Administration and the API browser open inside the shell. `X_FRAME_OPTIONS` is `SAMEORIGIN`.
- Logout is a POST that sends the CSRF token.
- The browser tunnel keeps reading guacd after the handshake, so a quiet RDP desktop still takes keyboard and mouse.
- LDAP extras are `django-auth-ldap` 5.4 and `python-ldap` 3.4. See `guacozy_server/requirements-ldap.txt`, `requirements-ldap-win.txt`, and `guacozy_server/wheelswin/readme.txt`. The old cp37 Windows wheels are gone.
- Agent access over MCP, below.

## Agents

Create a personal token on the start panel after you sign in. The secret is shown once. Revoke it on the same panel.

Send it to `POST /mcp/` as `Authorization: Bearer <secret>`. Session cookies are not accepted there. The existing nginx `location /` already proxies that path.

The token acts as that user. Folders the user cannot see stay hidden. Creating, changing, and deleting connections, and granting or revoking folder access, require a staff user. A connection with credential passthrough stays closed, because the agent has no browser-session password. A view-only shared ticket can take a snapshot and cannot click or type.

Open a desktop with `guacozy_open_desktop` and either a connection id or a ticket id. Call `guacozy_snapshot` before `guacozy_click`, `guacozy_type`, or `guacozy_press`. Clicks use pixels from that PNG. The origin is the top left. There are no element refs. After `STALE_SESSION`, call `guacozy_open_desktop` again.

You and the agent share the guacd session, so both see the same screen. The agent socket lives in memory on the Daphne process that accepted the open.

Connection tools never return a password, private key, or passphrase. A blank password on update keeps the stored password.

| Tool | What it does |
| --- | --- |
| `guacozy_whoami` | The user this token acts as |
| `guacozy_list_folders`, `guacozy_create_folder`, `guacozy_update_folder`, `guacozy_delete_folder` | Folders this user may see. Delete refuses a folder that still has children |
| `guacozy_list_connections`, `guacozy_get_connection`, `guacozy_create_connection`, `guacozy_update_connection`, `guacozy_delete_connection` | RDP, VNC, and SSH connections. Writes are staff-only |
| `guacozy_set_folder_access` | Grant or revoke one user or one group on a folder. Staff only |
| `guacozy_list_tickets`, `guacozy_create_ticket`, `guacozy_delete_ticket` | Tickets this user authors or holds |
| `guacozy_open_desktop` | Join a visible connection or ticket |
| `guacozy_snapshot` | Current frame as a PNG |
| `guacozy_click`, `guacozy_type`, `guacozy_press`, `guacozy_scroll` | Pointer and keyboard |
| `guacozy_clipboard_get`, `guacozy_clipboard_set` | Clipboard text for this desktop |
| `guacozy_close_desktop` | Close the agent socket. The shared guacd session id stays on the ticket |

Failures look like `{"ok": false, "error": {"code", "message", "retry"}}`. The codes are `INVALID_ARGS`, `NOT_ALLOWED`, `NOT_FOUND`, `STALE_SESSION`, and `REMOTE_DESKTOP`. `retry` is `safe`, `unsafe`, or `never`.

`AGENTS.md` has the call sequence and the local test commands.

## Work on the code

Python commands run from `guacozy_server` so Django collects `backend/tests.py`. Use the repo-root `.venv` (CPython 3.13). `manage.py check` needs `DJANGO_SECRET_KEY` and `FIELD_ENCRYPTION_KEY` in the environment or in `guacozy_server/.env`. `manage.py test` fills test-only values when they are missing.

GitHub Actions runs the same check, the test suite, and the frontend production build.

```shell
../.venv/bin/pip install -r requirements.txt
../.venv/bin/python manage.py check
../.venv/bin/python manage.py test
```

Frontend commands run from `frontend`. The passing build used Node 26.

```shell
npm ci
npm run build
```
