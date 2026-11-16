from django.test import TestCase

from backend.agent.tokens import authenticate_bearer, create_agent_token, hash_token
from backend.models import AgentToken
from users.models import User


class AgentTokenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="agent-owner", password="secret")
        self.other = User.objects.create_user(username="agent-other", password="secret")

    def test_create_lists_hash_and_revoke(self):
        self.client.force_login(self.user)
        created = self.client.post("/api/agent-tokens/", {"name": "laptop"}, content_type="application/json")
        self.assertEqual(created.status_code, 201)
        created_body = created.json()
        self.assertEqual(set(created_body.keys()), {"id", "name", "token", "created"})
        secret = created_body["token"]
        stored = AgentToken.objects.get(name="laptop")
        self.assertEqual(stored.token_hash, hash_token(secret))
        self.assertNotIn(secret, stored.token_hash)

        create_agent_token(self.other, "other-laptop")
        listed_response = self.client.get("/api/agent-tokens/")
        self.assertNotIn(secret, listed_response.content.decode())
        listed = listed_response.json()
        self.assertEqual([row["name"] for row in listed], ["laptop"])
        self.assertNotIn("token", listed[0])
        self.assertEqual(
            set(listed[0].keys()),
            {"id", "name", "created", "last_used_at", "revoked_at"},
        )
        self.assertEqual(authenticate_bearer("Bearer " + secret).username, self.user.username)
        self.assertIsNotNone(AgentToken.objects.get(pk=listed[0]["id"]).last_used_at)
        self.client.post(f"/api/agent-tokens/{listed[0]['id']}/revoke/")
        self.assertIsNotNone(AgentToken.objects.get(pk=listed[0]["id"]).revoked_at)
        self.assertIsNone(authenticate_bearer("Bearer " + secret))

        self.client.force_login(self.other)
        denied = self.client.post(f"/api/agent-tokens/{listed[0]['id']}/revoke/")
        self.assertEqual(denied.status_code, 404)
