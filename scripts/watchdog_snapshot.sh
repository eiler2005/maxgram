#!/usr/bin/env bash
# Собирает полный снимок конфигурации внешнего watchdog с обоих хостов.
#
# Результат — deploy/external-watchdog/CONFIGURATION.local.md: он содержит
# реальные адреса и отпечатки, поэтому лежит в .gitignore и в публичный git не
# попадает. Обобщённая карта настроек без значений — CONFIGURATION.md рядом.
#
# Секреты не выводятся: только длина и SHA-256 первых байт, чтобы можно было
# сверить, что на обоих хостах лежит одно и то же значение.
#
#   PROD_HOST=deploy@<ip> OBSERVER_HOST=deploy@<ip> ./scripts/watchdog_snapshot.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${HERE}/deploy/external-watchdog/CONFIGURATION.local.md"

: "${PROD_HOST:?PROD_HOST is required (deploy@<prod_ip>)}"
: "${OBSERVER_HOST:?OBSERVER_HOST is required (deploy@<observer_ip>)}"

ssh_q() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$@"; }

# Отпечаток секрета: длина + короткий хеш. Само значение не покидает хост.
FINGERPRINT='while IFS="=" read -r k v; do [ -n "$k" ] || continue;
  printf "| %s | %s | %s |\n" "$k" "${#v}" "$(printf "%s" "$v" | sha256sum | cut -c1-12)"; done'

{
  echo "# Снимок конфигурации внешнего watchdog"
  echo
  echo "Собран $(date -u '+%Y-%m-%d %H:%M UTC') скриптом \`scripts/watchdog_snapshot.sh\`."
  echo "**Файл содержит реальные адреса — он в \`.gitignore\`, не коммить.**"
  echo
  echo "Секреты представлены длиной и первыми 12 символами SHA-256: этого хватает,"
  echo "чтобы сверить совпадение значений на двух хостах, и не хватает, чтобы их узнать."
  echo
  echo "## Production-хост"
  echo
  echo '```'
  ssh_q "$PROD_HOST" 'hostname; echo "bridge: $(docker ps --filter name=deploy-bridge-1 --format "{{.Status}}")"; echo "push timer: $(systemctl is-active maxtg-watchdog-push.timer)"; echo "ports: $(docker port deploy-bridge-1 | tr "\n" " ")"'
  echo '```'
  echo
  echo "### .env.secrets — отпечатки watchdog-ключей"
  echo
  echo "| Ключ | Длина | SHA-256 (12) |"
  echo "|---|---|---|"
  ssh_q "$PROD_HOST" "grep -E '^(BRIDGE_STATUS_TOKEN|WATCHDOG_PUSH_SECRET)=' /opt/maxtg-bridge/.env.secrets | $FINGERPRINT"
  echo
  echo "### WATCHDOG_PUSH_URL"
  echo
  echo '```'
  ssh_q "$PROD_HOST" "grep '^WATCHDOG_PUSH_URL=' /opt/maxtg-bridge/.env.secrets"
  echo '```'
  echo
  echo "### status_api в config.local.yaml"
  echo
  echo '```yaml'
  ssh_q "$PROD_HOST" "grep -A4 '^status_api:' /opt/maxtg-bridge/config.local.yaml"
  echo '```'
  echo
  echo "### authorized_keys (ключи скрыты)"
  echo
  echo '```'
  ssh_q "$PROD_HOST" 'sed "s/AAAA[A-Za-z0-9+/=]*/<key>/g" ~/.ssh/authorized_keys'
  echo '```'
  echo
  echo "### UFW"
  echo
  echo '```'
  ssh_q "$PROD_HOST" 'sudo ufw status | grep -E "18151|22/tcp" || true'
  echo '```'
  echo
  echo "## Хост-наблюдатель"
  echo
  echo '```'
  ssh_q "$OBSERVER_HOST" 'hostname; echo "watchdog: $(docker ps --filter name=maxtg-watchdog --format "{{.Status}}")"; echo "ports: $(docker port maxtg-watchdog | tr "\n" " ")"'
  echo '```'
  echo
  echo "### .env.secrets — несекретные значения"
  echo
  echo '```'
  ssh_q "$OBSERVER_HOST" "grep -E '^WATCHDOG_(TARGET|CONTAINER|SSH|POLL|STATUS|PUSH_BIND|PUSH_PORT|DAILY)' /opt/maxtg-watchdog/.env.secrets"
  echo '```'
  echo
  echo "### .env.secrets — отпечатки секретов"
  echo
  echo "| Ключ | Длина | SHA-256 (12) |"
  echo "|---|---|---|"
  ssh_q "$OBSERVER_HOST" "grep -E '^(WATCHDOG_PUSH_SECRET|TG_BOT_TOKEN)=' /opt/maxtg-watchdog/.env.secrets | $FINGERPRINT"
  echo
  echo "> \`WATCHDOG_PUSH_SECRET\` обязан совпадать с production: сверь хеши выше."
  echo
  echo "### Ключ наблюдателя"
  echo
  echo '```'
  ssh_q "$OBSERVER_HOST" 'ls -ln /opt/maxtg-watchdog/secrets/watchdog_key'
  ssh-keygen -lf "${HERE}/deploy/external-watchdog/secrets/watchdog_key.pub" 2>/dev/null || echo "локальной копии ключа нет"
  echo '```'
  echo
  echo "### UFW"
  echo
  echo '```'
  ssh_q "$OBSERVER_HOST" 'sudo ufw status | grep 18151 || true'
  echo '```'
} > "$OUT"

chmod 600 "$OUT"
echo "Снимок сохранён: $OUT ($(wc -l < "$OUT") строк, режим 600)"
