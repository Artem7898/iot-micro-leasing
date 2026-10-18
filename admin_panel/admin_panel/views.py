"""Authenticated, read-only operations metrics; no credentials are rendered."""

import logging
from datetime import timedelta
from decimal import Decimal

import redis
from devices.models import Device, UsageSession
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.db import DatabaseError
from django.db.models import Count, F, Q, Sum
from django.shortcuts import render
from django.utils import timezone

logger = logging.getLogger(__name__)


def redis_snapshot():
    pool = None
    client = None
    try:
        pool = redis.ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
            health_check_interval=30,
            decode_responses=True,
        )
        client = redis.Redis(connection_pool=pool)
        info = client.info()
        hits = max(0, int(info.get("keyspace_hits", 0)))
        misses = max(0, int(info.get("keyspace_misses", 0)))
        total = hits + misses
        # These are lifetime Redis counters, not billing or HTTP request totals.
        hit_percent = round(hits / total * 100, 1) if total else 0.0
        return {
            "used_memory_human": str(info.get("used_memory_human", "Unknown")),
            "connected_clients": max(0, int(info.get("connected_clients", 0))),
            "total_commands_processed": max(0, int(info.get("total_commands_processed", 0))),
            "keyspace_hits": hits,
            "keyspace_misses": misses,
            "hit_rate": hit_percent,
            "uptime_display": str(timedelta(seconds=max(0, int(info.get("uptime_in_seconds", 0))))),
        }
    except redis.RedisError, ValueError, TypeError, OSError:
        logger.exception("Redis metrics could not be read")
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except redis.RedisError, OSError:
                logger.warning("Redis client cleanup failed", exc_info=True)
        if pool is not None:
            try:
                pool.disconnect()
            except redis.RedisError, OSError:
                logger.warning("Redis connection pool cleanup failed", exc_info=True)


def database_snapshot():
    try:
        devices = Device.objects.aggregate(
            device_count=Count("id"),
            active_device_count=Count("id", filter=Q(is_active=True)),
        )
        sessions = UsageSession.objects.aggregate(
            active_session_count=Count("id", filter=Q(is_active=True)),
            completed_session_count=Count("id", filter=Q(is_active=False)),
            total_cost=Sum("total_cost"),
        )
        recent = list(
            UsageSession.objects.order_by(
                F("last_ping_at").desc(nulls_last=True), "-created_at"
            ).values("id", "device_id", "total_cost", "is_active", "last_ping_at", "created_at")[
                :10
            ]
        )
        return {
            **devices,
            "active_session_count": sessions["active_session_count"],
            "completed_session_count": sessions["completed_session_count"],
            "total_cost_display": f"${Decimal(sessions['total_cost'] or 0):,.4f}",
            "recent_sessions": recent,
        }
    except DatabaseError:
        logger.exception("Application database metrics could not be read")
        return None


@staff_member_required(login_url="admin:login")
def redis_metrics_view(request):
    redis_metrics = redis_snapshot()
    dashboard_metrics = database_snapshot()
    hit_percent = redis_metrics["hit_rate"] if redis_metrics else 0.0
    # Keep HTML style attributes restricted to numeric percentages.
    redis_hit_percent = min(100.0, max(0.0, hit_percent))
    redis_miss_percent = (
        (100.0 - redis_hit_percent)
        if redis_metrics and (redis_metrics["keyspace_hits"] + redis_metrics["keyspace_misses"])
        else 0.0
    )
    context = {
        **admin.site.each_context(request),
        "title": "Operations dashboard",
        "redis_metrics": redis_metrics,
        "dashboard_metrics": dashboard_metrics,
        "redis_available": redis_metrics is not None,
        "database_available": dashboard_metrics is not None,
        "redis_error": "Redis metrics are temporarily unavailable. Check the service connection.",
        "database_error": "Application metrics are temporarily unavailable. Check database initialization.",
        "redis_hit_percent": redis_hit_percent,
        "redis_miss_percent": round(redis_miss_percent, 1),
        "generated_at": timezone.now(),
    }
    return render(request, "admin/redis_metrics.html", context)
