#!/usr/bin/env bash
# Деплой внешнего watchdog на второй VPS (хост-наблюдатель).
#
# Репозиторий vps_management сознательно не деплоит приложения, поэтому владелец
# приложения раскладывает свой watchdog сам — как это уже делают соседние стеки
# на том же хосте.
#
# Реальные адреса не хранятся в git: хост берётся из env или из untracked-файла
# deploy/external-watchdog/.deploy.env.
#
#   WATCHDOG_DEPLOY_HOST=deploy@<observer_ip> ./deploy/external-watchdog/deploy.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
REMOTE_DIR="${WATCHDOG_REMOTE_DIR:-/opt/maxtg-watchdog}"

if [[ -f "${HERE}/.deploy.env" ]]; then
  # shellcheck disable=SC1091
  source "${HERE}/.deploy.env"
fi

if [[ -z "${WATCHDOG_DEPLOY_HOST:-}" ]]; then
  echo "WATCHDOG_DEPLOY_HOST is required (deploy@<observer_ip>)" >&2
  exit 2
fi

if [[ ! -f "${HERE}/.env.secrets" ]]; then
  echo "Missing ${HERE}/.env.secrets — скопируй watchdog.env.example и заполни." >&2
  exit 2
fi

if [[ ! -f "${HERE}/secrets/watchdog_key" ]]; then
  echo "Missing ${HERE}/secrets/watchdog_key — выделенный ключ наблюдателя → production-хост." >&2
  echo "Создать: ssh-keygen -t ed25519 -N '' -f ${HERE}/secrets/watchdog_key" >&2
  exit 2
fi

SSH_CONTROL="/tmp/maxtg-watchdog-deploy-$$"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=${SSH_CONTROL}" -o ControlPersist=60)
cleanup() { ssh "${SSH_OPTS[@]}" -O exit "${WATCHDOG_DEPLOY_HOST}" 2>/dev/null || true; }
trap cleanup EXIT

run_remote() { ssh "${SSH_OPTS[@]}" "${WATCHDOG_DEPLOY_HOST}" "$@"; }

echo "==> Готовлю ${REMOTE_DIR} на ${WATCHDOG_DEPLOY_HOST}"
run_remote "sudo mkdir -p ${REMOTE_DIR}/secrets && sudo chown -R \$(id -u):\$(id -g) ${REMOTE_DIR} && chmod 700 ${REMOTE_DIR}/secrets"

echo "==> Копирую исходники и compose"
# tar через ssh вместо rsync: на хосте-наблюдателе rsync может отсутствовать,
# а ставить пакеты ради деплоя одного контейнера не хочется.
# Каталог с кодом пересоздаётся целиком, чтобы не оставлять устаревшие модули.
run_remote "rm -rf ${REMOTE_DIR}/src/watchdog_external && mkdir -p ${REMOTE_DIR}/src"
tar -C "${REPO_ROOT}/src" -czf - watchdog_external \
  | run_remote "tar -C ${REMOTE_DIR}/src -xzf -"
tar -C "${HERE}" -czf - Dockerfile docker-compose.yml \
  | run_remote "tar -C ${REMOTE_DIR} -xzf -"

echo "==> Копирую секреты (0600)"
# Ключ принадлежит uid контейнера, поэтому перед перезаписью возвращаем его себе.
run_remote "test -f ${REMOTE_DIR}/secrets/watchdog_key && sudo chown \$(id -u):\$(id -g) ${REMOTE_DIR}/secrets/watchdog_key || true"
scp "${SSH_OPTS[@]}" -q "${HERE}/.env.secrets" "${WATCHDOG_DEPLOY_HOST}:${REMOTE_DIR}/.env.secrets"
scp "${SSH_OPTS[@]}" -q "${HERE}/secrets/watchdog_key" "${WATCHDOG_DEPLOY_HOST}:${REMOTE_DIR}/secrets/watchdog_key"
run_remote "chmod 600 ${REMOTE_DIR}/.env.secrets ${REMOTE_DIR}/secrets/watchdog_key"
# Контейнер работает под непривилегированным uid 10001 (см. Dockerfile). Ключ
# должен принадлежать именно ему: иначе ssh внутри контейнера не сможет его
# прочитать и упадёт с "Permission denied (publickey)" — сообщение, которое
# выглядит как проблема с самим ключом, хотя дело в правах на файл.
run_remote "sudo chown ${WATCHDOG_UID:-10001}:${WATCHDOG_UID:-10001} ${REMOTE_DIR}/secrets/watchdog_key"

echo "==> Сборка и запуск"
run_remote "cd ${REMOTE_DIR} && docker compose build --quiet && docker compose up -d"

echo "==> Проверка"
run_remote "cd ${REMOTE_DIR} && docker compose ps"
run_remote "cd ${REMOTE_DIR} && docker compose exec -T watchdog python -m src.watchdog_external --once || true"

BUILD_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
run_remote "echo ${BUILD_SHA} > ${REMOTE_DIR}/BUILD_INFO"

echo "==> Готово. Тест доставки в Telegram:"
echo "    ssh ${WATCHDOG_DEPLOY_HOST} 'cd ${REMOTE_DIR} && docker compose exec -T watchdog python -m src.watchdog_external --test-alert'"
