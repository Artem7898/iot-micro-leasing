from django.db import models

class Device(models.Model):
    """
    Эта таблица физически создается миграциями SQLModel (FastAPI).
    Django только ЧИТАЕТ её для админки.
    """
    class Meta:
        managed = False # <--- ОЧЕНЬ ВАЖНО! Django не будет делать migrate для этой таблицы
        db_table = 'device' # Имя таблицы должно совпадать с SQLModel

    id = models.CharField(primary_key=True, max_length=36)
    rate_per_unit = models.DecimalField(max_digits=10, decimal_places=4)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

class UsageSession(models.Model):
    class Meta:
        managed = False
        db_table = 'usagesession'

    id = models.UUIDField(primary_key=True)
    device_id = models.ForeignKey(Device, on_delete=models.DO_NOTHING, db_column='device_id')
    total_cost = models.DecimalField(max_digits=12, decimal_places=4)
    is_active = models.BooleanField(default=True)
    last_ping_at = models.DateTimeField(null=True, blank=True)