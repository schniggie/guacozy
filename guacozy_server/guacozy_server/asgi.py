"""
ASGI entrypoint. Serves Django over HTTP and the Guacamole ticket tunnel
over WebSocket. Daphne and `manage.py runserver` both load this module.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "guacozy_server.settings")

# HTTP handler must be built before importing routing: that import pulls in
# models, which need Django to be set up already.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter

from .routing import websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
