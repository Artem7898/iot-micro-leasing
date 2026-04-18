from django.contrib.admin import AdminSite
from django.shortcuts import render
import redis


def redis_metrics_view(request):
    """Кастомная страница админки для отображения метрик Redis"""
    try:
        # Подключаемся к локальному Redis
        r = redis.from_url('redis://localhost:6379/0')
        info = r.info()

        # Достаем красивые метрики
        context = {
            'title': 'Redis Metrics',
            'used_memory_human': info.get('used_memory_human', 'N/A'),
            'connected_clients': info.get('connected_clients', 0),
            'total_commands_processed': info.get('total_commands_processed', 0),
            'keyspace_hits': info.get('keyspace_hits', 0),
            'keyspace_misses': info.get('keyspace_misses', 0),
            'is_connected': True
        }
    except Exception as e:
        context = {
            'title': 'Redis Metrics',
            'is_connected': False,
            'error': str(e)
        }

    # Используем стандартный шаблон админки Django (совместим с Unfold)
    return render(request, 'admin/redis_metrics.html', context)