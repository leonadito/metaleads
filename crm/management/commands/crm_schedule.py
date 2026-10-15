from django.core.management.base import BaseCommand

from crm.chatwoot_actions import ensure_waiting_check
from crm.messaging import ensure_queue_loop
from crm.meta import ensure_hourly_poll
from crm.orphans import ensure_nightly_check
from crm.tasks import ensure_daily_sync


class Command(BaseCommand):
    help = "Garante que as tarefas periódicas estão na fila (rodado na subida do container)."

    def handle(self, *args, **options):
        self.stdout.write("sincronização diária " + ("agendada" if ensure_daily_sync() else "já estava na fila"))
        self.stdout.write("leitura da Meta de hora em hora " + ("agendada" if ensure_hourly_poll() else "já estava na fila"))
        self.stdout.write("checagem noturna de órfãos " + ("agendada" if ensure_nightly_check() else "já estava na fila"))
        self.stdout.write("avisos de espera no Chatwoot " + ("agendados" if ensure_waiting_check() else "já estavam na fila"))
        self.stdout.write("fila de mensagens " + ("agendada" if ensure_queue_loop() else "já estava na fila"))
