
import pytest
import httpx
from typing import AsyncGenerator

# No application imports at module level


@pytest.fixture(scope="session")
def test_settings():
    """
    Фикстура, которая создает и возвращает "тестовый" объект настроек.
    Использует scope="session" для создания одного объекта на всю тестовую сессию.
    """
    # moved import inside fixture
    from config import Settings
    
    return Settings(
        BOT_TOKEN="12345:test",
        ADMIN_ID=12345,
        WEBHOOK_URL="https://test.dev/telegram",
        BASE_URL="https://test.dev",
        CACHE_DB_PATH=":memory:", # Используем БД в памяти для тестов
        DOWNLOADS_DIR="downloads_test",
        LOG_LEVEL="DEBUG",
        GENRE_DATA_PATH="genres.json",
        COOKIES_FILE="cookies_test.txt",
        COOKIES_CONTENT="",
        PLAY_MAX_FILE_SIZE_MB=50,
        TRACK_MIN_DURATION_S=60,
        TRACK_MAX_DURATION_S=900,
        GENRE_MIN_DURATION_S=1200,
        GENRE_MAX_DURATION_S=18000,
        MAX_RESULTS=5, # Уменьшаем кол-во результатов для ускорения тестов
        DOWNLOAD_RETRY_ATTEMPTS=1,
    )

@pytest.fixture
async def client(test_settings) -> AsyncGenerator[httpx.AsyncClient, None]:
    """
    Основная фикстура для создания тестового клиента.
    Она переопределяет зависимость настроек ПЕРЕД созданием клиента
    и очищает ее ПОСЛЕ завершения теста.
    """
    # moved imports inside fixture
    from main import app
    from dependencies import get_settings_dep
    from config import Settings

    def get_test_settings_override() -> Settings:
        return test_settings

    app.dependency_overrides[get_settings_dep] = get_test_settings_override

    # Используем правильный синтаксис с ASGITransport
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c

    # Очищаем переопределение после теста
    app.dependency_overrides.clear()
