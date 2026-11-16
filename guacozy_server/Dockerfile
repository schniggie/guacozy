FROM python:3.13-alpine
WORKDIR /app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# install psycopg2 dependencies
# The official Python image already ships its own headers. Alpine's
# python3-dev package is a second interpreter and must not be installed.
RUN apk update && apk add \
    build-base \
    ca-certificates \
    musl-dev \
    libpq-dev \
    libffi-dev \
    openldap-dev \
    cyrus-sasl-dev

COPY requirements*.txt /app/

RUN pip install --upgrade pip && \
    pip install  -r requirements-ldap.txt

# Django service
EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

