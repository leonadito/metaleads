"""Background tasks of the CRM (django-tasks, database backend; run by the
container's `db_worker`)."""
import logging
from datetime import timedelta

from django.utils import timezone
from django_tasks import task
from django_tasks_db.models import DBTaskResult

from .core_sync import full_sync

logger = logging.getLogger(__name__)

DAILY_SYNC_PATH = "crm.tasks.sync_tenants_daily"


def ensure_daily_sync():
    """Enqueues the daily tenant sync unless one is already waiting/running.
    Called at container start (crm_schedule command); the task then keeps
    re-enqueueing itself."""
    if DBTaskResult.objects.filter(task_path=DAILY_SYNC_PATH, status__in=["READY", "RUNNING"]).exists():
        return False
    sync_tenants_daily.enqueue()
    return True


@task
def sync_tenants_daily():
    """Full copy of the core's tenants, then schedules the next run: in 24 h,
    or in 1 h when the core could not be reached."""
    wait = timedelta(hours=24)
    try:
        count = full_sync()
        logger.info("Sincronização diária: %s tenant(s) do core", count)
        return count
    except Exception:
        wait = timedelta(hours=1)
        logger.exception("Sincronização diária com o core falhou; nova tentativa em 1 h")
        raise
    finally:
        sync_tenants_daily.using(run_after=timezone.now() + wait).enqueue()
