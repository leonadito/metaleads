import logging
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class LeadsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'leads'

    def ready(self):
        import os
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        # Django dev server chama ready() duas vezes (reloader + worker).
        # RUN_MAIN='true' só existe no processo worker real.
        # Under Django dev server, skip the reloader parent process.
        # Under Gunicorn (production), RUN_MAIN is not set — allow scheduler to start.
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            return

        from .tasks import check_new_leads
        from .services.telegram import poll_and_reply_start

        scheduler = BackgroundScheduler()
        scheduler.add_job(
            check_new_leads,
            trigger=IntervalTrigger(minutes=5),
            id='check_new_leads',
            replace_existing=True,
        )
        scheduler.add_job(
            poll_and_reply_start,
            trigger=IntervalTrigger(seconds=30),
            id='poll_telegram',
            replace_existing=True,
        )
        scheduler.start()
        logger.info("APScheduler iniciado: check_new_leads a cada 1 min, poll_telegram a cada 30s")
