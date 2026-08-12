FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# 1. Устанавливаем ffmpeg, build-essential (критично для ytdlp-jsc) и Node.js
RUN apt-get update && apt-get install -y \
    ffmpeg \
    build-essential \
    curl \
    ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 2. Устанавливаем зависимости (ytdlp-jsc успешно скомпилируется благодаря build-essential)
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
