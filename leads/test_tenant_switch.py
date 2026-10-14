import uuid

from django.contrib.auth.models import User
from django.contrib.sessions.backends.db import SessionStore
from django.test import RequestFactory, TestCase, override_settings

from .models import OIDCSession, UserProfile
from .oidc import CoreOIDCBackend
from .tenant_switch import client_switcher
from .test_oidc import OIDC_SETTINGS


def agency_claims(own, clients, sub=None, staff=False):
    return {
        "sub": sub or str(uuid.uuid4()), "email": "dona@alfa.com", "agitare_staff": staff,
        "memberships": [] if staff else [{
            "scope": "whitelabel", "whitelabel_id": str(uuid.uuid4()), "client_id": None, "role": "owner",
            "clients": [{"id": i, "slug": s, "name": n} for i, s, n in [own, *clients]],
        }],
    }


@override_settings(
    **OIDC_SETTINGS,
    AUTHENTICATION_BACKENDS=["leads.oidc.CoreOIDCBackend", "django.contrib.auth.backends.ModelBackend"],
)
class ClientSwitchTests(TestCase):
    def setUp(self):
        self.own = (str(uuid.uuid4()), "alfa", "Agência Alfa")
        self.c1 = (str(uuid.uuid4()), "chesini", "Chesini")
        self.c2 = (str(uuid.uuid4()), "novo", "Cliente Novo")
        self.chesini = User.objects.create_user(username="chesini")  # existing account, linked by slug

    def sso_login(self, claims):
        """Runs the backend like mozilla-django-oidc does, then logs in."""
        backend = CoreOIDCBackend()
        backend.request = RequestFactory().get("/")
        backend.request.session = SessionStore()
        self.assertTrue(backend.verify_claims(claims))
        users = backend.filter_users_by_claims(claims)
        user = backend.update_user(users[0], claims) if len(users) == 1 else backend.create_user(claims)
        self.client.force_login(user, backend="leads.oidc.CoreOIDCBackend")
        session = self.client.session
        session.update(backend.request.session.items())
        session["oidc_id_token"] = "id-token"
        session.save()
        OIDCSession.objects.create(sub=claims["sub"], session_key=session.session_key)
        return user

    def current_user(self):
        return User.objects.get(pk=self.client.session["_auth_user_id"])

    def test_agency_login_lands_on_its_own_tenant_account(self):
        user = self.sso_login(agency_claims(self.own, [self.c1, self.c2]))
        self.assertEqual(str(user.profile.core_client_uuid), self.own[0])
        self.assertEqual(len(self.client.session["tenant_choices"]), 3)

    def test_switch_signs_into_the_client_account_keeping_sso_data(self):
        claims = agency_claims(self.own, [self.c1])
        self.sso_login(claims)
        old_key = self.client.session.session_key
        response = self.client.post("/cliente/", {"client": self.c1[0]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.current_user(), self.chesini)
        self.assertEqual(str(self.chesini.profile.core_client_uuid), self.c1[0])
        session = self.client.session
        self.assertEqual(session["oidc_sub"], claims["sub"])
        self.assertEqual(session["oidc_id_token"], "id-token")
        self.assertEqual(len(session["tenant_choices"]), 2)
        # The single logout now targets the new session, not the old one.
        self.assertTrue(OIDCSession.objects.filter(session_key=session.session_key, sub=claims["sub"]).exists())
        self.assertFalse(OIDCSession.objects.filter(session_key=old_key).exists())

    def test_client_without_account_gets_one_on_switch(self):
        self.sso_login(agency_claims(self.own, [self.c2]))
        self.client.post("/cliente/", {"client": self.c2[0]})
        self.assertEqual(self.current_user().username, "novo")
        self.assertEqual(str(UserProfile.objects.get(user__username="novo").core_client_uuid), self.c2[0])

    def test_client_outside_the_list_is_refused(self):
        self.sso_login(agency_claims(self.own, [self.c1]))
        before = self.current_user()
        response = self.client.post("/cliente/", {"client": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.current_user(), before)

    def test_agitare_staff_enters_a_client_account_and_comes_back(self):
        UserProfile.objects.create(user=self.chesini, core_client_uuid=self.c1[0])
        staff = self.sso_login(agency_claims(self.own, [], staff=True))
        self.client.post("/cliente/", {"client": self.c1[0]})
        self.assertEqual(self.current_user(), self.chesini)
        self.client.post("/cliente/", {"client": "self"})
        self.assertEqual(self.current_user(), staff)

    def test_selector_needs_two_or_more_choices(self):
        request = RequestFactory().get("/")
        request.user = self.chesini
        request.session = {"tenant_choices": [{"id": self.c1[0], "name": "Chesini"}]}
        self.assertEqual(client_switcher(request)["client_choices"], [])
