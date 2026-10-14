from django.core.management.base import BaseCommand

from crm.tasks import ensure_daily_sync


class Command(BaseCommand):
    help = "Garante que a sincronização diária de tenants está na fila (rodado na subida do container)."

    def handle(self, *args, **options):
        self.stdout.write("sincronização diária " + ("agendada" if ensure_daily_sync() else "já estava na fila"))
