## Step 0

Install Docker Compose v2. From the repo root, copy the environment file and fill in the three required values. The comments in `.env.example` show how to generate them.

```shell
cp .env.example .env
```

`DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEY`, and `POSTGRES_PASSWORD` must be set. `POSTGRES_PASSWORD` must be letters and digits. Compose will not start while any of them is empty.

`DJANGO_ALLOWED_HOSTS` defaults to `localhost,127.0.0.1`. Add every hostname you open in the browser.

## Step 1

The repo already contains `docker-compose.yml`. It builds this tree. It does not use the older `guacozy/guacozy-server` image on Docker Hub.

```shell
docker compose up --build
```

The stack is:

- `server` — nginx, Daphne, and the built React app, published on `10443`
- `guacd` — `guacamole/guacd:1.6.0`
- `db` — Postgres 18 on the volume `postgres18-data`, mounted at `/var/lib/postgresql`

Port 80 is not published. Open:

- https://127.0.0.1:10443/cozy/ when Docker is on this machine
- https://your-docker-host:10443/cozy/ when it is on another host

The browser warns about the first self-signed certificate. That pair is kept on the `guacozy-ssl` volume.

## Step 2

A superuser is created only when `.env` sets `SUPERUSER_PASSWORD`. The username defaults to `admin` and the email to `admin@example.com`. There is no built-in `admin` / `admin` account.

Sign in with the account you configured, or create one with:

```shell
docker compose exec server ./manage.py createsuperuser
```

## Step 3

Leave the stack running with:

```shell
docker compose up --build -d
```

Read [Environment variables](environment-variables.md) before you point this at a database that already has connections. That database must keep its current `FIELD_ENCRYPTION_KEY`. A Postgres volume created with trust authentication does not pick up `POSTGRES_PASSWORD` on its own.
