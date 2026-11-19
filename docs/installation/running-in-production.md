If this is a production environment, make appropriate changes:

### Provide custom SSL certificates
You can provide your own certificates, read more in [SSL section](ssl.md)

### Published port
The compose file publishes `10443` for HTTPS. Port 80 is not published. Clipboard sync needs HTTPS when the host is not localhost.

### Set DJANGO_SECRET_KEY
Required. Use 50 or more characters. Do not use `$`, because Compose expands it.

### Set FIELD_ENCRYPTION_KEY
Required. Generate one before the first start and keep it. The server exits when the variable is empty. It does not create a replacement on startup.

If the database already holds connection passwords, reuse the key those passwords were written with. A different key makes them unreadable.

Generate a new key on a machine that already has the Python environment:

```shell
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Once the stack is running you can also print a new key with:

```shell
docker compose exec server ./manage.py generate_encryption_key
```

Put the value you intend to keep into `.env` before you store any passwords.

### Postgres password
`POSTGRES_PASSWORD` is required and must be letters and digits. Postgres uses it when the data volume is first created. A volume created earlier with trust authentication keeps that setting. The password in `.env` does not change it. Set the role password and `pg_hba` yourself, or start a new volume.
