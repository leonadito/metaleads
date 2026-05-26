import logging

from django.core.management.base import BaseCommand
from leads.services.telegram import poll_and_reply_start

logging.basicConfig(level=logging.DEBUG)


class Command(BaseCommand):
    help = 'Busca mensagens novas do Telegram e responde /start com o Chat ID'

    def handle(self, *args, **options):
        self.stdout.write('Buscando updates do Telegram...')
        poll_and_reply_start()
        self.stdout.write(self.style.SUCCESS('Concluído.'))
