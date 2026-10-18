from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import redis
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from admin_panel.settings import database_config, security_config

from .models import Device, UsageSession


@override_settings(ALLOWED_HOSTS=["testserver"])
class DashboardTests(TestCase):
    """Exercise actual admin pages against isolated unmanaged billing tables."""

    @classmethod
    def setUpClass(cls):
        # Django intentionally never migrates tables owned by SQLModel.
        with connection.schema_editor() as editor:
            editor.create_model(Device)
            editor.create_model(UsageSession)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        with connection.schema_editor() as editor:
            editor.delete_model(UsageSession)
            editor.delete_model(Device)

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user(
            username="dashboard-staff", is_staff=True, password=None
        )
        cls.regular_user = get_user_model().objects.create_user(
            username="regular-user", password=None
        )

    def setUp(self):
        self.pool_patch = patch("admin_panel.views.redis.ConnectionPool.from_url")
        self.redis_patch = patch("admin_panel.views.redis.Redis")
        self.pool = self.pool_patch.start().return_value
        self.redis_client = self.redis_patch.start().return_value
        self.addCleanup(self.pool_patch.stop)
        self.addCleanup(self.redis_patch.stop)
        self.redis_client.info.return_value = {
            "used_memory_human": "1.5M",
            "connected_clients": 3,
            "total_commands_processed": 120,
            "keyspace_hits": 6,
            "keyspace_misses": 2,
            "uptime_in_seconds": 900,
        }

    def get_dashboard(self):
        self.client.force_login(self.staff)
        return self.client.get(reverse("operations_dashboard"))

    def test_anonymous_cannot_read_metrics(self):
        response = self.client.get(reverse("operations_dashboard"))
        self.assertRedirects(
            response, "/admin/login/?next=/admin/dashboard/", fetch_redirect_response=False
        )
        self.redis_client.info.assert_not_called()

    def test_non_staff_cannot_read_metrics(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse("operations_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.redis_client.info.assert_not_called()

    def test_empty_dashboard_reports_actual_zero_totals(self):
        response = self.get_dashboard()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operations dashboard")
        metrics = response.context["dashboard_metrics"]
        self.assertEqual(metrics["device_count"], 0)
        self.assertEqual(metrics["active_session_count"], 0)
        self.assertEqual(metrics["total_cost_display"], "$0.0000")
        self.assertEqual(metrics["recent_sessions"], [])
        self.assertEqual(response.context["redis_metrics"]["hit_rate"], 75.0)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_populated_dashboard_aggregates_and_shows_activity(self):
        now = timezone.now()
        active = Device.objects.create(
            id="dev_active01", rate_per_unit=Decimal("0.5"), is_active=True, created_at=now
        )
        Device.objects.create(
            id="dev_blocked01", rate_per_unit=Decimal("0.5"), is_active=False, created_at=now
        )
        UsageSession.objects.create(
            id=str(uuid4()),
            device=active,
            total_cost=Decimal("1.2500"),
            is_active=True,
            last_ping_at=now,
            created_at=now,
        )
        UsageSession.objects.create(
            id=str(uuid4()),
            device=active,
            total_cost=Decimal("2.5000"),
            is_active=False,
            created_at=now,
        )
        response = self.get_dashboard()
        metrics = response.context["dashboard_metrics"]
        self.assertEqual(metrics["device_count"], 2)
        self.assertEqual(metrics["active_device_count"], 1)
        self.assertEqual(metrics["active_session_count"], 1)
        self.assertEqual(metrics["completed_session_count"], 1)
        self.assertEqual(metrics["total_cost_display"], "$3.7500")
        self.assertEqual(len(metrics["recent_sessions"]), 2)
        self.assertContains(response, "dev_active01")

    @override_settings(
        REDIS_URL="redis://localhost:6380/3",
        REDIS_CONNECT_TIMEOUT=0.4,
        REDIS_SOCKET_TIMEOUT=0.8,
        REDIS_MAX_CONNECTIONS=2,
    )
    def test_redis_uses_configured_bounded_pool_and_releases_resources(self):
        self.get_dashboard()
        from admin_panel.views import redis as redis_module

        redis_module.ConnectionPool.from_url.assert_called_once_with(
            "redis://localhost:6380/3",
            max_connections=2,
            socket_connect_timeout=0.4,
            socket_timeout=0.8,
            health_check_interval=30,
            decode_responses=True,
        )
        self.redis_client.close.assert_called_once()
        self.pool.disconnect.assert_called_once()

    def test_redis_timeout_has_generic_error_and_releases_pool(self):
        self.redis_client.info.side_effect = redis.TimeoutError("private connection details")
        with self.assertLogs("admin_panel.views", level="ERROR"):
            response = self.get_dashboard()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["redis_available"])
        self.assertContains(response, "Redis metrics are temporarily unavailable")
        self.assertNotContains(response, "private connection details")
        self.redis_client.close.assert_called_once()
        self.pool.disconnect.assert_called_once()

    def test_redis_connection_failure_is_logged_and_not_exposed(self):
        self.redis_client.info.side_effect = redis.ConnectionError("redis://user:secret@internal")
        with self.assertLogs("admin_panel.views", level="ERROR"):
            response = self.get_dashboard()
        self.assertFalse(response.context["redis_available"])
        self.assertNotContains(response, "user:secret")

    def test_database_failure_has_generic_error(self):
        with (
            patch(
                "admin_panel.views.Device.objects.aggregate",
                side_effect=OperationalError("private database path"),
            ),
            self.assertLogs("admin_panel.views", level="ERROR"),
        ):
            response = self.get_dashboard()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["database_available"])
        self.assertContains(response, "Application metrics are temporarily unavailable")
        self.assertNotContains(response, "private database path")

    def test_redis_zero_lookups_does_not_invent_hit_rate(self):
        self.redis_client.info.return_value = {"keyspace_hits": 0, "keyspace_misses": 0}
        response = self.get_dashboard()
        self.assertEqual(response.context["redis_hit_percent"], 0)
        self.assertEqual(response.context["redis_miss_percent"], 0)

    def test_dynamic_content_is_html_escaped(self):
        now = timezone.now()
        device = Device.objects.create(
            id="<script>alert(1)</script>", rate_per_unit=Decimal("1"), created_at=now
        )
        UsageSession.objects.create(
            id=str(uuid4()), device=device, total_cost=Decimal("1"), created_at=now
        )
        self.redis_client.info.return_value["used_memory_human"] = "<img src=x onerror=alert(1)>"
        response = self.get_dashboard()
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertNotContains(response, "<img src=x onerror=alert(1)>")

    def test_staff_can_view_records_but_cannot_write_them(self):
        device = Device.objects.create(
            id="dev_readonly1", rate_per_unit=Decimal("1"), created_at=timezone.now()
        )
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/admin/devices/device/").status_code, 200)
        change_url = reverse("admin:devices_device_change", args=[device.pk])
        self.assertEqual(self.client.get(change_url).status_code, 200)
        self.assertEqual(self.client.post(change_url, {"is_active": ""}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:devices_device_add"), {}).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("admin:devices_device_delete", args=[device.pk]), {"post": "yes"}
            ).status_code,
            403,
        )
        device.refresh_from_db()
        self.assertTrue(device.is_active)

    def test_admin_index_links_to_dashboard(self):
        self.client.force_login(self.staff)
        response = self.client.get("/admin/")
        self.assertContains(response, reverse("operations_dashboard"))


class DatabaseConfigurationTests(SimpleTestCase):
    def test_absolute_sqlite_url_matches_api(self):
        config = database_config("sqlite:////tmp/iot.sqlite3")
        self.assertEqual(str(config["NAME"]), "/tmp/iot.sqlite3")

    def test_postgres_url_keeps_tls_and_decodes_credentials(self):
        config = database_config(
            "postgresql+asyncpg://iot%40user:p%40ss@db.example:5433/iot?sslmode=verify-full"
        )
        self.assertEqual(config["USER"], "iot@user")
        self.assertEqual(config["PASSWORD"], "p@ss")
        self.assertEqual(config["PORT"], "5433")
        self.assertEqual(config["OPTIONS"]["sslmode"], "verify-full")

    def test_postgres_connect_timeout_is_bounded_and_keeps_tls(self):
        with patch.dict("os.environ", {"DB_CONNECT_TIMEOUT": "8"}):
            config = database_config(
                "postgresql://localhost/iot?sslmode=verify-full&sslrootcert=%2Ftmp%2Froot.crt"
            )
            self.assertEqual(config["OPTIONS"]["connect_timeout"], 8)
            self.assertEqual(config["OPTIONS"]["sslmode"], "verify-full")
            self.assertEqual(config["OPTIONS"]["sslrootcert"], "/tmp/root.crt")
            explicit = database_config("postgresql://localhost/iot?connect_timeout=3")
            self.assertEqual(explicit["OPTIONS"]["connect_timeout"], 3)
        for invalid in ("0", "31", "-1", "not-an-integer"):
            with self.subTest(timeout=invalid), self.assertRaises(ImproperlyConfigured):
                database_config(f"postgresql://localhost/iot?connect_timeout={invalid}")

    def test_postgres_default_connect_timeout_and_native_tls_environment(self):
        with patch.dict("os.environ", {"DB_CONNECT_TIMEOUT": "5", "PGSSLMODE": "verify-full"}):
            config = database_config("postgresql://localhost/iot")
        self.assertEqual(config["OPTIONS"]["connect_timeout"], 5)
        # Omitting sslmode here lets psycopg honor PostgreSQL's PGSSL* environment.
        self.assertNotIn("sslmode", config["OPTIONS"])

    def test_unsupported_url_and_tls_options_fail_explicitly(self):
        with self.assertRaises(ImproperlyConfigured):
            database_config("mysql://localhost/iot")
        with self.assertRaises(ImproperlyConfigured):
            database_config("postgresql://localhost/iot?unsupported=1")


class ProductionSecurityTests(SimpleTestCase):
    def production_values(self):
        return {
            "ENVIRONMENT": "production",
            "DJANGO_SECRET_KEY": "test-only-production-secret-value-" * 3,
            "DJANGO_ALLOWED_HOSTS": "leasing.example.com",
        }

    def test_production_defaults_disable_debug_and_enable_secure_flags(self):
        config = security_config(self.production_values())
        self.assertFalse(config["DEBUG"])
        for flag in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT"):
            self.assertTrue(config[flag])

    def test_production_rejects_debug_override(self):
        values = self.production_values() | {"DJANGO_DEBUG": "true"}
        with self.assertRaisesMessage(ImproperlyConfigured, "DJANGO_DEBUG must be false"):
            security_config(values)

    def test_production_requires_strong_nonfallback_secret(self):
        for secret in ("", "too-short", "x" * 60, "django-insecure-" + "test-only-value-" * 5):
            with self.subTest(secret_length=len(secret)):
                values = self.production_values() | {"DJANGO_SECRET_KEY": secret}
                with self.assertRaisesMessage(ImproperlyConfigured, "strong DJANGO_SECRET_KEY"):
                    security_config(values)
        values = self.production_values()
        values.pop("DJANGO_SECRET_KEY")
        with self.assertRaises(ImproperlyConfigured):
            security_config(values)

    def test_production_requires_explicit_host_allowlist(self):
        for hosts in ("", "*", "*.example.com"):
            with (
                self.subTest(hosts=hosts),
                self.assertRaisesMessage(ImproperlyConfigured, "explicit DJANGO_ALLOWED_HOSTS"),
            ):
                security_config(self.production_values() | {"DJANGO_ALLOWED_HOSTS": hosts})
        values = self.production_values()
        values.pop("DJANGO_ALLOWED_HOSTS")
        with self.assertRaises(ImproperlyConfigured):
            security_config(values)

    def test_production_rejects_insecure_cookie_or_redirect_overrides(self):
        for flag in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT"):
            with (
                self.subTest(flag=flag),
                self.assertRaisesMessage(ImproperlyConfigured, f"DJANGO_{flag} must be true"),
            ):
                security_config(self.production_values() | {f"DJANGO_{flag}": "false"})

    def test_development_defaults_still_support_local_http(self):
        config = security_config({"ENVIRONMENT": "development"})
        self.assertTrue(config["DEBUG"])
        self.assertFalse(config["SECURE_SSL_REDIRECT"])
