FROM python:3.12-slim

WORKDIR /app

# Непривилегированный пользователь для рантайма контейнера
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --create-home --home-dir /home/app app

# Зависимости отдельным слоем — кешируются при неизменном requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Исходники
COPY src/ ./src/
COPY config.yaml .
COPY spike/ ./spike/

# data/ монтируется как volume снаружи (docker-compose или Fly.io persistent volume)
# Создаём директорию на случай если volume не примонтирован
RUN mkdir -p /app/data/tmp /tmp/bridge-tmp && \
    chown -R app:app /app /tmp/bridge-tmp

ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1
ENV TMPDIR=/tmp/bridge-tmp

USER app

CMD ["python", "src/main.py"]
