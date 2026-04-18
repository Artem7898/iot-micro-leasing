from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.decorators import display
from .models import Device, UsageSession

@admin.register(Device)
class DeviceAdmin(ModelAdmin):
    list_display = ('id', 'rate_per_unit', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('id',)
    # Unfold автоматически добавит красивые фильтры, экспорт в CSV и UX

@admin.register(UsageSession)
class UsageSessionAdmin(ModelAdmin):
    list_display = ('id', 'device_id', 'total_cost', 'is_active')

from django.urls import reverse