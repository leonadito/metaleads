import uuid

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from .models import UserProfile
from .oidc import CoreOIDCBackend

OIDC_SETTINGS = dict(
    OIDC_RP_CLIENT_ID="metaleads-test",
    OIDC_RP_CLIENT_SECRET="x",
    OIDC_OP_AUTHORIZATION_ENDPOINT="https://id.example/o/authorize/",
    OIDC_OP_TOKEN_ENDPOINT="https://id.example/o/token/",
    OIDC_OP_USER_ENDPOINT="https://id.example/o/userinfo/",
    OIDC_OP_JWKS_ENDPOINT="https://id.example/o/.well-known/jwks.json",
    OIDC_RP_SIGN_ALGO="RS256",
)


def claims(email, sub=None, staff=False, client=None):
    memberships = []
    if client:
        memberships.append({"scope": "client", "client_id": client[0], "client_slug": client[1],
                            "client_name": client[1], "whitelabel_id": str(uuid.uuid4()), "role": "owner"})
    return {"sub": sub or str(uuid.uuid4()), "email": email, "given_name": "Leo", "family_name": "D",
            "agitare_staff": staff, "memberships": memberships}


@override_settings(**OIDC_SETTINGS)
class CoreOIDCBackendTests(TestCase):
    def setUp(self):
        self.backend = CoreOIDCBackend()

    def login(self, c):
        if not self.backend.verify_claims(c):
            return None
        users = self.backend.filter_users_by_claims(c)
        if len(users) == 1:
            return self.backend.update_user(users[0], c)
        return self.backend.create_user(c)

    def test_existing_client_account_is_linked_by_slug_case_insensitive(self):
        local = User.objects.create_user(username="Parigi", password="old")
        client_id = str(uuid.uuid4())
        user = self.login(claims("parigi@agitare.com.br", client=(client_id, "parigi")))
        self.assertEqual(user.pk, local.pk)
        self.assertEqual(str(user.profile.core_client_uuid), client_id)
        self.assertTrue(user.check_password("old"))
        self.assertEqual(User.objects.count(), 1)

    def test_people_of_the_same_client_share_its_account(self):
        client = (str(uuid.uuid4()), "chesini")
        a = self.login(claims("ana@chesini.com", client=client))
        b = self.login(claims("bia@chesini.com", client=client))
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(a.username, "chesini")
        self.assertFalse(a.has_usable_password())
        self.assertEqual(a.email, "")  # the shared account keeps its own e-mail

    def test_linked_account_is_not_relinked_to_another_client(self):
        other = User.objects.create_user(username="sol")
        UserProfile.objects.create(user=other, core_client_uuid=uuid.uuid4())
        user = self.login(claims("x@sol.com", client=(str(uuid.uuid4()), "sol")))
        self.assertNotEqual(user.pk, other.pk)
        self.assertEqual(user.username, "sol2")

    def test_agitare_staff_gets_a_personal_superuser(self):
        sub = str(uuid.uuid4())
        user = self.login(claims("leoduarte@ipmasters.com.br", sub=sub, staff=True))
        again = self.login(claims("leoduarte@ipmasters.com.br", sub=sub, staff=True))
        self.assertEqual(user.pk, again.pk)
        self.assertTrue(user.is_superuser and user.is_staff)
        self.assertEqual(str(user.profile.core_user_uuid), sub)

    def test_without_client_membership_the_login_is_refused(self):
        self.assertIsNone(self.login(claims("ninguem@x.com")))
        self.assertEqual(User.objects.count(), 0)
