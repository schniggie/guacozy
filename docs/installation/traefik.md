Guacozy can be run using Træfik reverse proxy

This is an example when Træfik is configured to use [docker as backend](https://docs.traefik.io/v1.4/configuration/backends/docker/)  
All træfik config is done using labels in container  
There is a network `proxy` which is used as a network connecting træfik to backend servers.

### Changes to docker-compose
Use external network (network is called `proxy` in this example) for `server` service
```
services:
  server:
    networks:
      - default
      - proxy
...

...
networks:
  proxy:
      external: true
```  
Specify træfik labels to automatically configure new frontend/backend in træfik  

```
    labels:
      traefik.port: 80
      traefik.enable: true
      traefik.frontend.rule: guacozy.example.com
```
Specify `DJANGO_ALLOWED_HOSTS` to be the same as host in træfik frontend
```
    environment:
      - DJANGO_ALLOWED_HOSTS=guacozy.example.com
```

### Working example
```
version: '3'
services:
  server:
    image: guacozy/guacozy-server
    restart: always
    depends_on:
      - db
    volumes:
      - ./config/ssl:/ssl/
      - ./config/ldap_config.py:/app/guacozy_server/ldap_config.py
    networks:
      - default
      - proxy
    labels:
      traefik.port: 80
      traefik.enable: true
      traefik.frontend.rule: guacozy.example.com
    environment:
      - DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY:?Set DJANGO_SECRET_KEY in .env}
      - FIELD_ENCRYPTION_KEY=${FIELD_ENCRYPTION_KEY:?Set FIELD_ENCRYPTION_KEY in .env}
      - DJANGO_DB_URL=postgres://postgres:${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}@db:5432/postgres
      - DJANGO_ALLOWED_HOSTS=guacozy.example.com
      - DEBUG=False
  guacd:
    image: guacamole/guacd:1.6.0
    restart: always
    networks:
      - default
  db:
    image: postgres:18-alpine
    restart: always
    environment:
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}
    volumes:
      - ./data/postgres18:/var/lib/postgresql
    networks:
      - default

networks:
  proxy:
      external: true
```
