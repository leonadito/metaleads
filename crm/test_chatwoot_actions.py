import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from . import chatwoot_actions as cw
from . import funnel
from .ingest import ingest
from .models import EventType, Lead, LeadEvent, Stage, Substage, Tenant


class FakeBot:
    """The account API as the bot sees it: labels, custom attributes,
    messages (private notes) and status per conversation."""

    def __init__(self):
        self.labels, self.attributes, self.notes, self.status, self.assignee = {}, {}, {}, {}, {}
        self.calls = []

    def __call__(self, tenant, method, path, payload=None):
        self.calls.append((method, path, payload))
        parts = path.split("/")
        cid = int(parts[1])
        tail = parts[2] if len(parts) > 2 else ""
        if tail == "labels":
            if method == "GET":
                return {"payload": list(self.labels.get(cid, []))}
            self.labels[cid] = list(payload["labels"])
            return {"payload": self.labels[cid]}
        if tail == "custom_attributes":
            self.attributes.setdefault(cid, {}).update(payload["custom_attributes"])
            return {}
        if tail == "messages":
            assert payload["private"] is True
            self.notes.setdefault(cid, []).append(payload["content"])
            return {}
        if tail == "toggle_status":
            self.status[cid] = payload["status"]
            return {}
        if tail == "":
            return {"id": cid, "status": self.status.get(cid, "open"), "meta": {"assignee": self.assignee.get(cid)}}
        raise AssertionError(f"inesperado {method} {path}")


@override_settings(CHATWOOT_URL="https://chat.example", CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg")
class ChatwootActionsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Pousada", slug="pousada", chatwoot_account_id=5,
                                            chatwoot_bot_token="tok-bot", chatwoot_admin_id=9, chatwoot_admin_name="Dona Ana")
        self.contato = Substage.objects.create(tenant=self.tenant, stage=Stage.EM_ANALISE, name="Primeiro contato", order=0)
        self.visita = Substage.objects.create(tenant=self.tenant, stage=Stage.EM_ANALISE, name="Visita", order=1)
        self.lead = ingest(self.tenant, {"source": "whatsapp", "phone": "51999998888", "conversation_id": 42}).lead
        self.bot = FakeBot()
        patcher = mock.patch.object(cw, "bot_api", self.bot)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_queued(self, path="crm.chatwoot_actions.sync_lead"):
        for result in DBTaskResult.objects.filter(task_path=path, status="READY"):
            getattr(cw, path.rsplit(".", 1)[1]).call(*result.args_kwargs["args"])
            result.delete()

    def move(self, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            r = funnel.move(self.lead, *args, **kwargs)
        self.run_queued()
        self.lead.refresh_from_db()
        return r

    def test_options_and_labels(self):
        self.assertEqual(cw.funnel_options(self.tenant), [
            "Criado", "Em análise > Primeiro contato", "Em análise > Visita", "Qualificado", "Não qualificado",
            "Convertido", "Perdido",
        ])
        self.assertEqual(cw.destination_for(self.tenant, "Em análise > Visita"), (Stage.EM_ANALISE, self.visita))
        self.assertEqual(cw.destination_for(self.tenant, "Qualificado"), (Stage.QUALIFICADO, None))
        self.assertIsNone(cw.destination_for(self.tenant, "Em análise"))  # has sub-stages
        self.assertIsNone(cw.destination_for(self.tenant, "Inventada"))

    def test_move_updates_the_attribute(self):
        self.move(substage=self.visita, actor="ana")
        self.assertEqual(self.bot.attributes[42], {"subetapa": "Em análise > Visita"})

    def test_review_and_release(self):
        self.move(substage=self.contato, actor="llm", by=funnel.AUTO)
        self.move(substage=self.visita, actor="llm", by=funnel.AUTO)
        self.bot.status[42] = "pending"  # with the bot
        r = self.move(substage=self.contato, actor="llm", reason="pediu outro horário", by=funnel.AUTO)
        self.assertEqual(r.status, "review")
        self.assertEqual(self.bot.labels[42], ["revisão"])
        self.assertEqual(self.bot.status[42], "open")
        note = self.bot.notes[42][-1]
        self.assertIn("revisão humana", note)
        self.assertIn("Vaivém", note)
        self.assertIn("Caminho recente", note)
        with self.captureOnCommitCallbacks(execute=True):
            funnel.release(self.lead, actor="ana")
        self.run_queued()
        self.assertEqual(self.bot.labels[42], [])

    def conversation_updated(self, value, previous=None, assignee=None):
        return {
            "event": "conversation_updated", "id": 42, "account": {"id": 5},
            "meta": {"assignee": assignee},
            "changed_attributes": [{"custom_attributes": {
                "previous_value": {"subetapa": previous} if previous else {}, "current_value": {"subetapa": value},
            }}],
        }

    def test_agent_sets_the_attribute_in_chatwoot(self):
        Lead.objects.filter(pk=self.lead.pk).update(frozen_at=timezone.now(), review_reason="x")
        with self.captureOnCommitCallbacks(execute=True):
            status = cw.conversation_changed.call(self.tenant.pk, self.conversation_updated(
                "Em análise > Visita", "Criado", assignee={"id": 3, "name": "Beto"}))
        self.assertEqual(status, "moved")
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.substage, self.lead.frozen_at), (self.visita, None))  # a person's move releases
        e = LeadEvent.objects.filter(type=EventType.STAGE_CHANGED).last()
        self.assertEqual((e.actor, e.data["by"]), ("Chatwoot (Beto)", "human"))
        self.assertIn("Definido no Chatwoot em", e.reason)

    def test_echo_and_unknown_values_are_ignored(self):
        self.move(substage=self.visita, actor="ana")
        self.assertEqual(cw.conversation_changed.call(self.tenant.pk, self.conversation_updated("Em análise > Visita")), "nada a mover")
        self.assertEqual(cw.conversation_changed.call(self.tenant.pk, self.conversation_updated("Inventada")), "subetapa desconhecida")

    def test_waiting_flag_and_alert(self):
        cw.message_seen(self.lead, "incoming", "contact")
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.waiting_since)
        cw.message_seen(self.lead, "outgoing", "agent_bot")  # the bot does not count as an answer
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.waiting_since)

        self.assertEqual(cw.due_waiting(), [])  # not 15 minutes yet
        later = timezone.now() + timedelta(minutes=16)
        due = cw.due_waiting(later)
        self.assertEqual(due, [self.lead])
        self.assertEqual(cw.alert_waiting(due[0], later), "avisado")
        self.assertEqual(self.bot.labels[42], ["aguardando-resposta"])
        self.assertIn("[@Dona Ana](mention://user/9/Dona Ana)", self.bot.notes[42][-1])
        self.assertEqual(cw.due_waiting(later + timedelta(minutes=5)), [])  # at most once per alert time
        self.bot.assignee[42] = {"id": 3, "name": "Beto"}
        again = cw.due_waiting(later + timedelta(minutes=16))
        cw.alert_waiting(again[0], later + timedelta(minutes=16))
        self.assertIn("mention://user/3/Beto", self.bot.notes[42][-1])

        self.lead.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            cw.message_seen(self.lead, "outgoing", "user")  # an agent answered
        self.run_queued("crm.chatwoot_actions.remove_waiting_label")
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.waiting_since)
        self.assertEqual(self.bot.labels[42], [])

    def test_no_alert_when_the_bot_has_the_conversation(self):
        cw.message_seen(self.lead, "incoming", "contact")
        self.bot.status[42] = "pending"
        lead = cw.due_waiting(timezone.now() + timedelta(minutes=20))[0]
        self.assertEqual(cw.alert_waiting(lead), "sem espera")
        self.assertNotIn(42, self.bot.notes)

    def test_resolved_conversation_ends_the_wait(self):
        cw.message_seen(self.lead, "incoming", "contact")
        cw.conversation_changed.call(self.tenant.pk, {"event": "conversation_status_changed", "id": 42, "status": "resolved"})
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.waiting_since)

    def test_alert_time_per_tenant(self):
        Tenant.objects.filter(pk=self.tenant.pk).update(waiting_alert_minutes=60)
        cw.message_seen(self.lead, "incoming", "contact")
        self.assertEqual(cw.due_waiting(timezone.now() + timedelta(minutes=30)), [])
        self.assertEqual(len(cw.due_waiting(timezone.now() + timedelta(minutes=61))), 1)


@override_settings(CHATWOOT_URL="https://chat.example", CORE_API_URL="https://core.example", CORE_WEBHOOK_SECRET="seg")
class ChatwootCredentialsTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(uuid=uuid.uuid4(), name="Pousada", slug="pousada", chatwoot_account_id=5)

    def test_token_is_read_from_the_core_and_again_on_401(self):
        core = mock.Mock(status_code=200)
        core.json.return_value = {"webhook_secret": "s", "bot_token": "tok-novo", "admin_user_id": 9, "admin_name": "Ana"}
        refused, ok = mock.Mock(status_code=401, text="", content=b""), mock.Mock(status_code=200, content=b"{}")
        ok.json.return_value = {}
        Tenant.objects.filter(pk=self.tenant.pk).update(chatwoot_bot_token="tok-velho")
        self.tenant.refresh_from_db()
        with mock.patch("crm.chatwoot_actions.requests.get", return_value=core) as get, \
                mock.patch("crm.chatwoot_actions.requests.request", side_effect=[refused, ok]) as request:
            cw.bot_api(self.tenant, "GET", "conversations/1/labels")
        self.assertEqual(get.call_count, 1)
        self.assertEqual([c.kwargs["headers"]["api_access_token"] for c in request.call_args_list], ["tok-velho", "tok-novo"])
        self.tenant.refresh_from_db()
        self.assertEqual((self.tenant.chatwoot_bot_token, self.tenant.chatwoot_admin_id), ("tok-novo", 9))

    def test_funnel_options_go_to_the_core(self):
        Substage.objects.create(tenant=self.tenant, stage=Stage.QUALIFICADO, name="Quente")
        answer = mock.Mock(status_code=200)
        with mock.patch("crm.chatwoot_actions.requests.post", return_value=answer) as post:
            cw.push_funnel_options.call(self.tenant.pk)
        url = post.call_args.args[0]
        self.assertTrue(url.endswith(f"/api/v1/tenants/{self.tenant.uuid}/chatwoot/subetapas"))
        self.assertIn("Qualificado > Quente", post.call_args.kwargs["json"]["options"])
