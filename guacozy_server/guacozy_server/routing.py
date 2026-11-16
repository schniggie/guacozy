from django.urls import path

from .guacdproxy import GuacamoleConsumer

websocket_urlpatterns = [
    path("tunnelws/ticket/<uuid:ticket>/", GuacamoleConsumer.as_asgi()),
]
