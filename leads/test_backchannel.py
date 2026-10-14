import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth.signals import user_logged_in
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import RequestFactory, TestCase, override_settings

from .backchannel import EVENT, backchannel_logout
from django.contrib.auth.models import User

from .models import OIDCSession

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()

SETTINGS = dict(
    OIDC_RP_CLIENT_ID="metaleads-beta",
    OIDC_RP_CLIENT_SECRET="x",
    OIDC_OP_AUTHORIZATION_ENDPOINT="https://id.example/o/authorize/",
    OIDC_OP_TOKEN_ENDPOINT="https://id.example/o/token/",
    OIDC_OP_USER_ENDPOINT="https://id.example/o/userinfo/",
    OIDC_OP_JWKS_ENDPOINT="https://id.example/o/.well-known/jwks.json",
    OIDC_RP_SIGN_ALGO="RS256",
    OIDC_RP_IDP_SIGN_KEY=PUBLIC_PEM,
    OIDC_OP_ISSUER="https://id.example/o",
)


def token(sub, key=KEY, **overrides):
    claims = {"iss": "https://id.example/o", "aud": "metaleads-beta", "iat": int(time.time()),
              "jti": str(uuid.uuid4()), "sub": sub, "events": {EVENT: {}}}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


@override_settings(**SETTINGS)
class BackchannelLogoutTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def open_session(self, sub):
        s = SessionStore()
        s["x"] = 1
        s.create()
        OIDCSession.objects.create(sub=sub, session_key=s.session_key)
        return s.session_key

    def post(self, logout_token):
        return backchannel_logout(self.factory.post("/oidc/backchannel-logout/", {"logout_token": logout_token}))

    def test_valid_token_ends_only_that_persons_sessions(self):
        ana, bia = str(uuid.uuid4()), str(uuid.uuid4())
        ana_1, ana_2, bia_1 = self.open_session(ana), self.open_session(ana), self.open_session(bia)
        self.assertEqual(self.post(token(ana)).status_code, 200)
        alive = set(Session.objects.values_list("session_key", flat=True))
        self.assertNotIn(ana_1, alive)
        self.assertNotIn(ana_2, alive)
        self.assertIn(bia_1, alive)
        self.assertEqual(OIDCSession.objects.count(), 1)

    def test_invalid_tokens_are_refused_and_end_nothing(self):
        sub = str(uuid.uuid4())
        key = self.open_session(sub)
        for bad in (
            token(sub, aud="OUTRO-APP"),                 # meant for another app
            token(sub, nonce="abc"),                     # an ID token, not a logout token
            token(sub, events=None),                     # no logout event
            token(sub, iss="https://evil.example/o"),    # another issuer
            token(sub, iat=int(time.time()) - 3600),     # too old
            token(sub, key=OTHER_KEY),                   # not signed by the core
            "nao-e-um-jwt",
        ):
            self.assertEqual(self.post(bad).status_code, 400, bad[:30])
        self.assertTrue(Session.objects.filter(session_key=key).exists())

    def test_sso_login_records_the_session_for_its_person(self):
        user = User.objects.create_user(username="ana", email="ana@x.com", password="x")
        request = self.factory.get("/")
        request.session = SessionStore()
        request.session["oidc_sub"] = sub = str(uuid.uuid4())
        request.session.create()
        user_logged_in.send(sender=User, request=request, user=user)
        self.assertEqual(str(OIDCSession.objects.get(session_key=request.session.session_key).sub), sub)

    def test_local_password_login_is_not_tracked(self):
        user = User.objects.create_user(username="ana", email="ana@x.com", password="x")
        request = self.factory.get("/")
        request.session = SessionStore()
        request.session.create()
        user_logged_in.send(sender=User, request=request, user=user)
        self.assertFalse(OIDCSession.objects.exists())
