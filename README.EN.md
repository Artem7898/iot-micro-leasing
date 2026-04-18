<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Django_5-092E20.svg?logo=django&logoColor=white" alt="Django 5">
  <img src="https://img.shields.io/badge/SQLModel-2C3E50.svg?logo=sqlite&logoColor=white" alt="SQLModel">
  <img src="https://img.shields.io/badge/Pytest-0A9EDC.svg?logo=pytest&logoColor=white" alt="Pytest">
</p>

<h1 align="center">🌐 IoT Micro-Leasing Platform</h1>

<p align="center">
  <em>IoT equipment micro-leasing: pay-per-use rental model</em>
</p>

---

## 📋 Description

An enterprise-grade monorepo implementing equipment rental contracts based on actual usage (event-driven billing), not time-based rental.

**Usage Examples:**
- 💰 Pay per printed page on a 3D printer
- 🚁 Pay per kilometer flown by an agricultural drone
- 📡 Pay per megabyte transmitted from an IoT sensor

## 🧠 Why This Architecture?

The application addresses challenges of high-load billing systems requiring:

| Requirement | Solution |
|-------------|----------|
| **Idempotency** | Protection against double-charging on unstable Wi-Fi (IoT devices often send duplicate "pings") |
| **Timestamp Validation** | Protection against replay attacks and handling delayed requests (long offline buffer on devices) |
| **High Load** | Millions of small requests with rate limiting and async processing |

### Hybrid Pattern

- 🚀 **FastAPI (API Contour)**: Handles load, validation (Pydantic v2), and async DB writes
- 🎨 **Django 5 + Unfold (Admin Contour)**: Back-office for managers. Does not load API, only reads tables, providing a modern React interface

___

#### Launch Infrastructure (Optional)
#### docker-compose up -d postgres redis
#### Start Services:
#### cd api uvicorn main:app --host 0.0.0.0 --port 8000 --reload
#### 🔗 Swagger UI: http://localhost:8000/api/docs
#### Redis (If not running as system service)
#### redis-server
#### Celery Worker (PDF Generator)
#### cd api celery -A api.core.celery_app.celery_app worker --loglevel=info -Q invoices
#### Django Admin Panel:
#### On first run, create Django tables and superuser:
- cd admin_panel
- python manage.py migrate
- python manage.py createsuperuser
- python manage.py runserver 8001
#### 🔗 Admin Panel: http://localhost:8001/admin/
#### 🔗 Redis Dashboard: http://localhost:8001/admin/redis-metrics/
#### 🧪 Testing:
#### Tests are fully isolated from external environment (no PostgreSQL or Redis required). Uses in-memory SQLite and dependency injection (DI) mocking.
#### pytest tests/ -v 

___

###  ⚙️ Technology Stack:
- SQLModel + AsyncPG: Unified ORM creating tables that Django reads
- Pydantic v2 (strict mode): Protection against garbage data from IoT devices
- Slowapi + Redis: Flexible DDoS and sensor flood protection (rate limiting)
- Structlog: Structured JSON logging (ready for ELK/Loki)
- Django Unfold: Modern administrative interface built on React

<p align="center">
  <sub>Built with ❤️ for IoT billing</sub>
</p>
```
Autor Artem Alimpiev 

## 🏗️ System Architecture

### Overall Contour Schema

```mermaid
flowchart TB
    subgraph Clients ["Clients"]
        IOT[IoT Devices]
        WEB[Web/Mobile SPA]
        MGR[Business Managers]
    end

    NGINX["Nginx Reverse Proxy"]

    subgraph FastAPI_Contour ["FastAPI Contour (Port 8000)"]
        UV[Uvicorn Workers]
        API[FastAPI App]
        SL[Slowapi Limiter]
        AUTH[JWT Auth]
        SQLM[SQLModel ORM]
    end

    subgraph Django_Contour ["Django Contour (Port 8001)"]
        GU[Gunicorn WSGI]
        DJ[Django 5 Core]
        UNF[Unfold Admin UI]
        DJORM[Django ORM managed=False]
    end

    subgraph Data_Layer ["Data Layer"]
        PG[(PostgreSQL 16)]
        RD[(Redis)]
    end

    IOT --&gt;|REST / WebSockets| NGINX
    WEB --&gt;|REST| NGINX
    MGR --&gt;|Browser| NGINX

    NGINX --&gt;|/api/*| UV
    NGINX --&gt;|/admin/*| GU

    UV --&gt; API
    API --&gt; SL
    API --&gt; AUTH
    API --&gt; SQLM
    SQLM &lt;--&gt;|read/write| PG
    SL &lt;--&gt;|rate limit store| RD

    GU --&gt; DJ
    DJ --&gt; UNF
    DJ --&gt; DJORM
    DJORM &lt;--&gt;|read-only| PG

