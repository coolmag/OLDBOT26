# Берем легкий образ Linux с Python 3.11
FROM python:3.11-slim

# Устанавливаем системные пакеты: ffmpeg (для звука) и nodejs (для yt-dlp)
RUN apt-get update && \
    apt-get install -y ffmpeg nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Создаем рабочую папку
WORKDIR /app

# Копируем список библиотек и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта
COPY . .

# Запускаем FastAPI сервер (Railway сам пробросит порт)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
