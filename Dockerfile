ARG BUILDFRONTENDFROM=node:26-alpine
ARG SERVERFROM=python:3.13-alpine

####################
# BUILDER FRONTEND #
####################

FROM ${BUILDFRONTENDFROM} as builder-frontend
ARG DOCKER_TAG
ADD frontend/package.json /frontend/
ADD frontend/package-lock.json /frontend/
ADD frontend/.npmrc /frontend/
WORKDIR /frontend
RUN npm install
ADD frontend /frontend
# src/index.jsx imports this file from outside /frontend.
COPY guacozy_server/static/css/styles.css /guacozy_server/static/css/styles.css
ENV REACT_APP_VERSION=$DOCKER_TAG
RUN npm run build

##################
# BUILDER WHEELS #
##################

FROM ${SERVERFROM} as builder-wheels

# set work directory
WORKDIR /usr/src/app

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

COPY guacozy_server/requirements*.txt ./
RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /usr/src/app/wheels -r requirements-ldap.txt

#########
# FINAL #
#########

FROM ${SERVERFROM}

COPY --from=builder-wheels /usr/src/app/wheels /wheels

# install dependencies
RUN apk update && apk add --no-cache \
      bash \
      libpq \
      libldap \
      libsasl \
      ca-certificates \
      openssl \
      memcached \
      nginx \
	  supervisor

# Inject built wheels and install them
COPY --from=builder-wheels /usr/src/app/wheels /wheels
RUN pip install --upgrade pip && \
    pip install --no-cache /wheels/*

# Inject django app
COPY guacozy_server  /app

# Inject built frontend
COPY --from=builder-frontend /frontend/build /frontend

# Inject docker specific configuration
COPY docker /tmp/docker

# Distribute configuration files and prepare dirs for pidfiles
RUN mkdir -p /run/nginx && \
    mkdir -p /run/daphne && \
    cd /tmp/docker && \
    mv entrypoint.sh /entrypoint.sh && \
    chmod +x /entrypoint.sh && \
    mv nginx-default.conf /etc/nginx/http.d/default.conf && \
    mkdir -p /etc/supervisor.d/ && \
    mv /tmp/docker/supervisor-app.ini /etc/supervisor.d/ && \
    mv /tmp/docker/supervisord.conf /etc/supervisord.conf && \
    # create /app/.env if doesn't exists for less noise from django-environ
    touch /app/.env

ENTRYPOINT ["/entrypoint.sh"]

# Change to app dir so entrypoint.sh can run ./manage.py and other things localy to django
WORKDIR /app

CMD ["supervisord", "-n"]
EXPOSE 80
EXPOSE 443