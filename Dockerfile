# Этап 1: Базовый образ с Python
FROM python:3.11-slim

# Отключаем буферизацию вывода Python, чтобы логи сразу попадали в консоль Railway
ENV PYTHONUNBUFFERED=1

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# Обновляем список пакетов и устанавливаем FFmpeg + современный Node.js
# Node.js 20.x критически важен для yt-dlp, чтобы он мог решать JavaScript-задачи YouTube
# (ошибки Signature solving failed и n challenge solving failed исчезнут)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл с зависимостями в контейнер
COPY requirements.txt .

# Устанавливаем зависимости
# Мы используем --no-cache-dir, чтобы не хранить лишние файлы и уменьшить размер образа
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта в контейнер
COPY . .

# Открываем порт, на котором будет работать бот. 
# Railway (и Yandex Serverless) ожидают, что приложение будет слушать порт 8080.
EXPOSE 8080

# Команда для запуска приложения при старте контейнера
# Запускаем uvicorn, чтобы он слушал все входящие подключения на порту 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
