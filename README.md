<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/FastAPI-009688.svg?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Django_5-092E20.svg?logo=django&logoColor=white" alt="Django 5">
  <img src="https://img.shields.io/badge/SQLModel-2C3E50.svg?logo=sqlite&logoColor=white" alt="SQLModel">
  <img src="https://img.shields.io/badge/Pytest-0A9EDC.svg?logo=pytest&logoColor=white" alt="Pytest">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT">
</p>

<h1 align="center">🌐 Платформа микролизинга для Интернета вещей</h1>

<p align="center">
  <em>Микролизинг IoT-оборудования: аренда с оплатой по факту использования</em>
</p>

## 📋 Описание

Монорепозиторий enterprise-уровня, реализующий договор аренды оборудования не по времени, а по факту использования (событийная оплата).

**Примеры использования:**
- 💰 Оплата за каждый распечатанный лист на 3D-принтере
- 🚁 Оплата за каждый пройденный километр арендованного дрона-агрария
- 📡 Оплата за мегабайты переданных данных с IoT-датчика

## 🧠 Почему эта архитектура?

Приложение решает проблему высоконагруженных биллинговых систем, где требуются:

| Требование | Решение |
|------------|---------|
| **Идемпотентность** | Защита от двойных списаний при нестабильном Wi-Fi (IoT-устройства часто шлют «пинг» несколько раз) |
| **Валидация timestamp** | Защита от Replay-атак и обработка запросов из прошлого (долгий оффлайн-буфер устройства) |
| **Высокая нагрузка** | Миллионы мелких запросов с ограничением скорости и асинхронной обработкой |

### Гибридный шаблон

- 🚀 **FastAPI (API-контур)**: принимает нагрузку, валидацию (Pydantic v2) и асинхронную запись в БД
- 🎨 **Django 5 + Unfold (Admin-контур)**: Back-office для менеджеров. Не нагружает API, только читает таблицы, предоставляя современный React-интерфейс

___

## 📁 Структура проекта

## 🚀 Быстрый старт (Локальная разработка)

### 1. Установка зависимостей

**Требуется Python 3.12+**
### 2. Настройка окружения
#### Создай файл .env в корне проекта на основе .env.example (укажи данные PostgreSQL и Redis, либо используй SQLite для быстрого старта).

### 3. Запуск инфраструктуры (опционально)
#### docker-compose up -d postgres redis
### 4. Запуск сервисов
#### FastAPI (API-шлюз):
#### cd api && uvicorn main:app --host 0.0.0.0 --port 8000 --reload
#### 🔗 Swagger UI: http://localhost:8000/api/docs
####  Redis (если не запущен как системный сервис)
#### redis-server
#### Celery Worker (Генератор PDF)
#### cd api celery -A api.core.celery_app.celery_app worker --loglevel=info -Q invoices
##### Панель администратора Django:
#### При первом запуске необходимо создать таблицы для Django и суперпользователя
- cd admin_panel
- python manage.py migrate
- python manage.py createsuperuser
- python manage.py runserver 8001
#### 🔗 Админка: http://localhost:8001/admin/
#### 🔗 Дашборд Redis: http://localhost:8001/admin/redis-metrics/

### 🧪 Тестирование
#### Тесты полностью изолированы от внешней среды (не требуют PostgreSQL или Redis). Используется in-memory SQLite и подмена зависимостей (DI)
#### pytest tests/ -v

___

### ⚙️ Технологический стек
- SQLModel + AsyncPG: Единая ORM, создающая таблицы, которые читает Django
- Pydantic v2 (строгий режим): защита от мусорных данных с IoT-устройств
- Slowapi + Redis: Гибкая защита от DDoS и флуда с датчиков (rate limiting)
- Structlog: Структурированное логирование в JSON (готово для ELK/Loki)
- Django Unfold: 

___

## 👨‍💻 Автор → Артем Алимпиев
## 📄 Лицензия с ссылкой на MIT

### Общая схема контуров

```mermaid
flowchart TB
    subgraph Clients ["Клиенты"]
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

    IOT -->|REST / WebSockets| NGINX
    WEB -->|REST| NGINX
    MGR -->|Browser| NGINX

    NGINX -->|/api/*| UV
    NGINX -->|/admin/*| GU

    UV --> API
    API --> SL
    API --> AUTH
    API --> SQLM
    SQLM <-->|read/write| PG
    SL <-->|rate limit store| RD

    GU --> DJ
    DJ --> UNF
    DJ --> DJORM
    DJORM <-->|read-only| PG

```bash
git clone <repo_url>
cd iot-micro-leasing
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Установка проекта в editable-режиме с dev-зависимостями
pip install -e ".[dev]"
