from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

# Вычисляем абсолютный путь к корню проекта (на 2 уровня вверх от текущего файла)
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Теперь он всегда ищет .env строго в корне монорепозитория
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore"  # Игнорировать лишние переменные в .env
    )

    # Даем дефолтные значения. Если .env нет — приложение не упадет при старте.
    # Если .env есть — эти значения перезапишутся из файла.
    DATABASE_URL: str = "postgresql+asyncpg://postgres:secret@localhost:5432/iot_leasing"
    REDIS_URL: str = "redis://localhost:6379/0"
    JWT_SECRET: str = "default_unsafe_key_for_tests"


settings = Settings()