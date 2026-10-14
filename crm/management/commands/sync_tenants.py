from django.core.management.base import BaseCommand

from crm.core_sync import full_sync


class Command(BaseCommand):
    help = "Copia todos os tenants do Agitare Core (sincronização completa; rodar uma vez por dia)."

    def handle(self, *args, **options):
        self.stdout.write(f"{full_sync()} tenant(s) sincronizado(s) com o core.")
