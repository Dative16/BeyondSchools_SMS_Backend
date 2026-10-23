"""Celery application and the scheduled jobs the plan depends on."""

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "bs_sms",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.workers.tasks.invoices",
        "app.workers.tasks.reconciliation",
        "app.workers.tasks.notifications",
        "app.workers.tasks.ai_sync",
    ],
)

celery_app.conf.update(
    timezone="Africa/Dar_es_Salaam",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "nightly-reconciliation": {
            "task": "app.workers.tasks.reconciliation.reconcile_previous_day",
            "schedule": crontab(hour=5, minute=0),
        },
        "fee-reminders": {
            "task": "app.workers.tasks.notifications.send_fee_reminders",
            "schedule": crontab(hour=8, minute=30, day_of_week="mon"),
        },
        "invoice-balance-audit": {
            "task": "app.workers.tasks.invoices.audit_balances",
            "schedule": crontab(hour=2, minute=0),
        },
    },
)
