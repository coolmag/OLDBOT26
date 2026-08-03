# Changelog

## [Unreleased] - 2026-08-03

### Added
- **Invidious Downloader**: Integrated Invidious as a primary source for downloading tracks. The new download pipeline is: Invidious -> SoundCloud -> Audius -> InternetArchive -> Jamendo. This enhances reliability by providing a robust fallback mechanism.

## [Phase 1: Foundation]
- Создан файл `CHANGELOG.md` для отслеживания изменений.
- Начало перехода к архитектуре с очередью задач (Celery/Redis) -> Отменено в пользу `asyncio.Queue` (асинхронная очередь в памяти).
- Внедрена Pre-flight проверка на DRM в `youtube.py` для предотвращения пустых загрузок.
- Настроена конфигурация `Dockerfile` и `start.sh` для деплоя бота с Celery воркером -> Оптимизировано под запуск без Celery.
- Реализована фоновая задача префетчинга (предварительной загрузки) треков с использованием `asyncio.Queue`.
- Внедрено SQLite кэширование метаданных треков через `db_service.py` для снижения нагрузки на API.
- Создан `event_bus.py` и внедрена событийная модель (Event-Driven) для взаимодействия между сервисами.

## [Future Phases]
- Полный рефакторинг обмена данными между сервисами через `EventBus`.
