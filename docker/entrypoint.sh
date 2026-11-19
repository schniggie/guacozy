#!/bin/bash
set -e

# Set CACHE_URL to use local memory
# This is needed because currently memcached is not started and we need cache inside management commands
export CACHE_URL=locmemcache://

if [ -z "${DJANGO_SECRET_KEY:-}" ] || [ -z "${FIELD_ENCRYPTION_KEY:-}" ]; then
  echo "DJANGO_SECRET_KEY and FIELD_ENCRYPTION_KEY must be set. See .env.example." >&2
  exit 1
fi

# wait shortly and then run db migrations (retry on error)
sleep 3
while ! python ./manage.py migrate 2>&1; do
  echo "Waiting on DB..."
  sleep 3
done

# Create a superuser only when a password was provided. An existing user is left as it is.
if [ -z "${SUPERUSER_PASSWORD:-}" ]; then
  echo "SUPERUSER_PASSWORD is unset. Skipping superuser creation."
else
  if [ -z "${SUPERUSER_NAME:-}" ]; then
    SUPERUSER_NAME=admin
  fi
  if [ -z "${SUPERUSER_EMAIL:-}" ]; then
    SUPERUSER_EMAIL=admin@example.com
  fi
  export SUPERUSER_NAME SUPERUSER_EMAIL SUPERUSER_PASSWORD
  echo "Ensuring superuser ${SUPERUSER_NAME} (${SUPERUSER_EMAIL}) exists."
  python ./manage.py shell --interface python << 'END'
import os
from django.contrib.auth import get_user_model
User = get_user_model()
name = os.environ["SUPERUSER_NAME"]
if not User.objects.filter(username=name).exists():
    User.objects.create_superuser(name, os.environ["SUPERUSER_EMAIL"], os.environ["SUPERUSER_PASSWORD"])
END
fi

# copy static files
python ./manage.py collectstatic --no-input

# init default groups
python ./manage.py initgroups
python ./manage.py initguacd


# Set CACHE_URL to use memcached via sock file
export CACHE_URL=memcache:///tmp/memcached.sock

# Nginx
# Generate a certificate only when both files are missing. Leave a pair that is already there.
echo " "
echo "Checking that SSL certificate and key files exist..."

if [ -f /ssl/cert.key ] && [ -f /ssl/cert.crt ]; then
  echo "Using the certificate already in /ssl."
elif [ -f /ssl/cert.key ] || [ -f /ssl/cert.crt ]; then
  echo "Provide both /ssl/cert.key and /ssl/cert.crt." >&2
  exit 1
else
  echo "Creating a self-signed certificate in /ssl."
  mkdir -p /ssl
  openssl req -x509 -nodes -days 3650 -subj "/CN=guacozy" -addext "subjectAltName=DNS:guacozy" -newkey rsa:4096 -keyout /ssl/cert.key -out /ssl/cert.crt
fi

echo "Initialisation is done."

# launch whatever is passed by docker
# (i.e. the RUN instruction in the Dockerfile)
exec ${@}
