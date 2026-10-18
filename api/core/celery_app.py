"""Bounded, explicitly discovered invoice workers and outbox scheduling."""

from celery import Celery

from api.core.config import settings

celery_app = Celery(
    "iot_leasing_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["api.tasks.generate_invoice"],
    # This worker uses SQLModel; a Django web process must not change its loader.
    fixups=[],
)

celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
    task_routes={"api.tasks.*": {"queue": "invoices"}},
    task_default_queue="invoices",
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_ignore_result=True,
    task_soft_time_limit=45,
    task_time_limit=60,
    task_publish_retry=True,
    task_publish_retry_policy={
        "max_retries": 3,
        "interval_start": 0,
        "interval_step": 0.5,
        "interval_max": 1,
    },
    worker_concurrency=2,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=100,
    worker_max_memory_per_child=131_072,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=3,
    broker_connection_timeout=3,
    broker_pool_limit=5,
    broker_transport_options={
        "visibility_timeout": 300,
        "socket_connect_timeout": 3,
        "socket_timeout": 3,
        "health_check_interval": 30,
        "retry_on_timeout": False,
    },
    redis_socket_connect_timeout=3,
    redis_socket_timeout=3,
    redis_retry_on_timeout=False,
    redis_max_connections=10,
    result_expires=3600,
    enable_utc=True,
    timezone="UTC",
    beat_schedule={
        "deliver-pending-invoices": {
            "task": "api.tasks.dispatch_pending_invoices",
            "schedule": 30.0,
            "options": {"queue": "invoices", "expires": 25},
        }
    },
)
