FROM python:3.11-slim

# Отключаем буферизацию, чтобы логи сразу шли в Railway
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 1. Устанавливаем FFmpeg и Node.js 20.x (критично для yt-dlp)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Проверяем, что Node.js точно встал в систему
RUN node --version

COPY requirements.txt .

# 2. Устанавливаем основные зависимости
RUN pip install --no-cache-dir -r requirements.txt

# 3. ПРИНУДИТЕЛЬНО ставим yt-dlp-ejs (решатель JS-задач YouTube)
# Это гарантирует, что он точно установится, даже если pip его пропустил
RUN pip install --no-cache-dir --force-reinstall yt-dlp-ejs

COPY . .

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
