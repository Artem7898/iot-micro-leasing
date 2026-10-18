from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Device, UsageSession


class ReadOnlyModelAdmin(ModelAdmin):
    """Billing records are changed through the API, never through the back office."""

    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_staff)


@admin.register(Device)
class DeviceAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "rate_per_unit", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("id",)
    readonly_fields = ("id", "rate_per_unit", "is_active", "created_at")


@admin.register(UsageSession)
class UsageSessionAdmin(ReadOnlyModelAdmin):
    list_display = ("id", "device_id", "total_cost", "is_active", "last_ping_at")
    list_filter = ("is_active",)
    search_fields = ("id", "device__id")
    readonly_fields = ("id", "device", "total_cost", "is_active", "last_ping_at", "created_at")
