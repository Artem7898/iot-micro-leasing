"""Read-only projections of tables owned by the API's SQLModel schema."""

from django.db import models


class Device(models.Model):
    id = models.CharField(primary_key=True, max_length=36)
    rate_per_unit = models.DecimalField(max_digits=10, decimal_places=4)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "device"

    def __str__(self):
        return self.id


class UsageSession(models.Model):
    id = models.CharField(primary_key=True, max_length=36)
    device = models.ForeignKey(Device, on_delete=models.DO_NOTHING, db_column="device_id")
    total_cost = models.DecimalField(max_digits=12, decimal_places=4)
    is_active = models.BooleanField(default=True)
    last_ping_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "usagesession"

    def __str__(self):
        return self.id
