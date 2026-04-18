from celery import Celery
from api.core.config import settings

# Создаем инстанс Celery, который использует тот же Redis
celery_app = Celery(
    "iot_leasing_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

celery_app.conf.task_routes = {
    'api.tasks.*': {'queue': 'invoices'},
}