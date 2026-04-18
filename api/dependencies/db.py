
from sqlmodel import Session
from api.core.config import settings

# В реальном приложении здесь будет engine = create_engine(settings.DATABASE_URL)
# Для тестов engine будет подменяться в фикстурах
def get_db_session():
    # Заглушка, чтобы main.py не падал при импорте
    raise NotImplementedError("DB Engine not initialized")