import hashlib
import secrets

from django.utils import timezone

from backend.models import AgentToken


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_agent_token(user, name: str) -> tuple[AgentToken, str]:
    secret = secrets.token_urlsafe(32)
    token = AgentToken.objects.create(
        user=user,
        name=name,
        token_hash=hash_token(secret),
    )
    return token, secret


def authenticate_bearer(authorization_header: str | None):
    if not isinstance(authorization_header, str):
        return None
    scheme, separator, raw = authorization_header.strip().partition(" ")
    if scheme != "Bearer" or separator != " " or not raw or " " in raw:
        return None
    try:
        token = AgentToken.objects.select_related("user").get(
            token_hash=hash_token(raw),
            revoked_at__isnull=True,
        )
    except AgentToken.DoesNotExist:
        return None
    # A revoke that lands after the read must not still authenticate.
    updated = AgentToken.objects.filter(
        pk=token.pk,
        revoked_at__isnull=True,
    ).update(last_used_at=timezone.now())
    if not updated:
        return None
    return token.user
