FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

# Установка Python, FFmpeg, Node.js и Deno
RUN apt-get update && apt-get install -y \
    python3 python3-pip ffmpeg curl unzip nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Установка Deno
RUN curl -fsSL https://deno.land/x/install/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
# Установка PyTorch с поддержкой CUDA
RUN pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
RUN pip install -r requirements.txt

COPY . .

CMD ["python", "main.py"]
