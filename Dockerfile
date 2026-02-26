# Используем легкий и надежный образ Python
FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Устанавливаем системные пакеты (ffmpeg критически важен для викторины)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем библиотеки Питона
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код нашего бота
COPY . .

# Railway сам подставит сюда порт (например, 8080)
# Запускаем Uvicorn напрямую
CMD uvicorn main:app --host 0.0.0.0 --port $PORT
