from django.core.management.base import BaseCommand

from crm.meta import ensure_hourly_poll
from crm.tasks import ensure_daily_sync


class Command(BaseCommand):
    help = "Garante que as tarefas periódicas estão na fila (rodado na subida do container)."

    def handle(self, *args, **options):
        self.stdout.write("sincronização diária " + ("agendada" if ensure_daily_sync() else "já estava na fila"))
        self.stdout.write("leitura da Meta de hora em hora " + ("agendada" if ensure_hourly_poll() else "já estava na fila"))
