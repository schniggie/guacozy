These variables are read from the environment. Compose loads them from a gitignored `.env` in the repo root. `manage.py` on the host reads `guacozy_server/.env`.

`DJANGO_SECRET_KEY` and `FIELD_ENCRYPTION_KEY` are required. The process exits when either is empty. Nothing in the repository fills them in.

`DEBUG` : Django DEBUG mode. Dev compose sets it true. Production leaves it unset, so debug is off.

`DJANGO_ALLOWED_HOSTS`: Host header allow list. Defaults to `localhost,127.0.0.1`.
([Read more](https://docs.djangoproject.com/en/6.1/ref/settings/#allowed-hosts))

`DJANGO_SECRET_KEY` : signing key. Use a long random string. Avoid `$`, which Compose would expand.

`FIELD_ENCRYPTION_KEY` : encrypts passwords in the database. [Read more](running-in-production.md)

`POSTGRES_PASSWORD` : database password for Compose. Letters and digits only.

`DJANGO_TIME_ZONE` : timezone (e.g. Europe/Vilnius)

`DJANGO_DB_URL` : database URL. See [django-environ](https://django-environ.readthedocs.io/en/latest/index.html). Compose sets it from `POSTGRES_PASSWORD`.

`SUPERUSER_NAME`, `SUPERUSER_EMAIL`, `SUPERUSER_PASSWORD` : when the password is set, startup creates that user if the username is missing. When the password is unset, startup creates nobody.

`CSRF_TRUSTED_ORIGINS` : optional comma-separated origins, including the scheme and port.

`SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` : optional. The default is on when `DEBUG` is off. The CSRF cookie stays readable by JavaScript so the API client can send it.
