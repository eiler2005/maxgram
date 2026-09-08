# Внешний watchdog для maxtg-bridge. Разворачивается НЕ на production-хосте bridge,
# а на втором VPS: наблюдатель обязан переживать смерть наблюдаемого.
FROM python:3.13-slim

WORKDIR /app

# openssh-client — единственная внешняя зависимость: слой 2 ходит SSH-пробой.
# Python-пакетов не ставим вообще: watchdog написан на стандартной библиотеке.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 watchdog \
    && useradd --system --uid 10001 --gid watchdog --create-home --home-dir /home/watchdog watchdog

COPY src/watchdog_external/ ./src/watchdog_external/
RUN touch ./src/__init__.py

RUN mkdir -p /app/state && chown -R watchdog:watchdog /app

ENV PYTHONUNBUFFERED=1
ENV WATCHDOG_STATE_PATH=/app/state/state.json

USER watchdog

# Собственный heartbeat наблюдателя: свежий файл = цикл проверок крутится.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import os,sys,time; p='/app/state/watchdog_heartbeat'; \
sys.exit(0 if os.path.exists(p) and time.time()-float(open(p).read().strip())<300 else 1)"

CMD ["python", "-m", "src.watchdog_external"]
