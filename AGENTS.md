# Guacozy agent guide

Run the Python commands from `guacozy_server`. Django discovers tests from that working directory, so these commands collect `backend/tests.py`. Use the repo-root `.venv` (CPython 3.13). Run the frontend commands from `frontend`. The passing frontend run used Node 26.13.1 and npm 11.20.0.

## Install the Python requirements

Working directory: `guacozy_server`

    ../.venv/bin/pip install -r requirements.txt

`requirements.txt` includes `requirements-base.txt`.

## manage.py check

Working directory: `guacozy_server`

    ../.venv/bin/python manage.py check

## In-repo Django tests

Working directory: `guacozy_server`

    ../.venv/bin/python manage.py test

## Frontend lockfile install

Working directory: `frontend`

    npm ci

## Frontend production build

Working directory: `frontend`

    npm run build

The production build writes `frontend/build/index.html` and a JS bundle under `frontend/build/assets/`.

Login and the logged-in shell share `guacozy_server/static/css/styles.css` (color, type, and spacing). The login template loads it through `guacozy_server/templates/base/base.html`. The React shell imports the same file from `frontend/src/index.jsx`.

## Use Guacozy over MCP

Create a token on the start panel. The secret is shown once.

Send it as `Authorization: Bearer` to `POST /mcp/`. Session cookies are not accepted there.

Call `guacozy_open_desktop` with either a connection id or a ticket id.

Call `guacozy_snapshot` before `guacozy_click`, `guacozy_type`, or `guacozy_press`. Clicks use pixels from that snapshot. Origin is top left. There are no element refs.

After `STALE_SESSION`, call `guacozy_open_desktop` again.
