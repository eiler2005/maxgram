# Runbook: Hetzner Production

> **С версии 1.1.7 рекомендуемый способ — [Ansible](../../infra/ansible/README.md).**
> Для регулярного апдейта используй `ansible-playbook deploy.yml`.
> Для бэкапа — `backup.yml`. Для нового VM — `bootstrap.yml` + `hardening.yml`.
> Ручные шаги ниже остаются источником правды для того, что Ansible намеренно
> не автоматизирует (создание VM в панели, копирование секретов, SMS reauth)
> и как fallback, если ansible недоступен.
> `ansible-playbook deploy.yml --check --diff` здесь означает preflight verify без rollout, а не полную симуляцию `docker compose build/up`.

## Цель

Безопасный production-деплой bridge на Hetzner Cloud:

- один VM
- Docker Compose
- без публичных HTTP-портов
- SSH только по ключу и только с доверенного IP
- `UFW` + `fail2ban` + `unattended-upgrades`
- секреты и state только на сервере

## Рекомендованный сервер

- Серия: `CX23`
- Регион: `hel1`
- ОС: `Ubuntu 24.04`
- Сеть: `Primary IPv4 + IPv6`
- Hetzner Backups: `on`

Ориентир по стоимости на 2026-04-02:

- `CX23`: `EUR 3.99 / month`
- `Primary IPv4`: `EUR 0.50 / month`
- `Backups`: `EUR 0.80 / month`
- Итого: `EUR 5.29 / month` без VAT
- При `19% VAT`: около `EUR 6.30 / month`

## Что подготовить локально

Нужно иметь локально:

- `.env.secrets`
- `.env`
- `config.local.yaml`
- `data/max_bridge_session*`
- `data/bridge.db`

Сделай бэкап:

```bash
tar -czf maxtg-bridge-backup.tgz .env .env.secrets config.local.yaml data/
```

## 1. Создать сервер в Hetzner

При создании:

- добавить SSH key
- включить Backups
- выбрать `hel1`
- создать Cloud Firewall

Firewall bootstrap:

- inbound `22/tcp` только с твоего IP, если IP стабильный
- если IP нестабильный, временно открыть `22/tcp`, потом закрыть
- других inbound-правил не добавлять

## 2. Базовый hardening

Под root:

```bash
adduser deploy
usermod -aG sudo deploy
rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy
```

В `/etc/ssh/sshd_config`:

```text
PermitRootLogin no
PasswordAuthentication no
PubkeyAuthentication yes
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM yes
X11Forwarding no
```

Потом:

```bash
systemctl restart ssh
```

Установить обновления и базовые пакеты:

```bash
apt update && apt upgrade -y
apt install -y unattended-upgrades ca-certificates curl git ufw
dpkg-reconfigure -plow unattended-upgrades
```

## 3. Установить Docker

```bash
apt install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker deploy
```

Перелогиниться.

## 4. Защитить SSH и сетевой доступ

```bash
apt install -y ufw fail2ban
```

Минимальный baseline:

- в `sshd`:
  - `PermitRootLogin no`
  - `PasswordAuthentication no`
  - `AllowUsers deploy`
- в `UFW`:
  - `default deny incoming`
  - `default allow outgoing`
  - `allow 22/tcp` только с домашнего IP
- в `fail2ban`:
  - включить jail `sshd`
  - добавить свой текущий IP в `ignoreip`

Cloud Firewall в панели Hetzner:

- не должно быть широких правил `Any IPv4` / `Any IPv6`
- должно быть только `22/tcp` с твоего IP в формате `x.x.x.x/32`

Важно: если домашний IP изменится, правило нужно обновить и в Hetzner Cloud Firewall, и в `UFW` на самом сервере.

## 5. Развернуть bridge

```bash
sudo mkdir -p /opt/maxtg-bridge
sudo chown deploy:deploy /opt/maxtg-bridge
cd /opt/maxtg-bridge
git clone <YOUR_GIT_REMOTE> .
cp deploy/hetzner.env.example .env.host
mkdir -p data
chmod 700 data
```

Скопировать на сервер:

- `.env.secrets`
- `.env`
- `config.local.yaml`
- содержимое `data/`

Права:

```bash
chmod 600 .env .env.secrets config.local.yaml .env.host
chmod 700 data
```

Выставить UID/GID:

```bash
echo "APP_UID=$(id -u)" > .env.host
echo "APP_GID=$(id -g)" >> .env.host
```

Первый запуск:

```bash
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml build
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml up -d
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs -f
```

## 6. Миграция MAX-сессии

Ожидаемый путь:

- bridge стартует
- использует перенесённую сессию
- подключается к MAX без re-auth

Если MAX требует re-auth:

```bash
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml down
docker run --rm -it \
  --env-file .env \
  --env-file .env.secrets \
  -e CONFIG_PATH=/app/config.yaml \
  -e CONFIG_LOCAL_PATH=/app/config.local.yaml \
  -e DATA_DIR=/app/data \
  -v "$(pwd)/data:/app/data" \
  -v "$(pwd)/config.yaml:/app/config.yaml:ro" \
  -v "$(pwd)/config.local.yaml:/app/config.local.yaml:ro" \
  maxtg-bridge:prod \
  python -m src.main
```

## 7. Проверка после деплоя

Проверить:

- `docker ps`
- `sudo ufw status numbered`
- `sudo fail2ban-client status sshd`
- `docker ps`
- `docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=100`
- startup-лог содержит `MAX connected`, затем `Running startup tests`, затем `Startup tests passed: ...`
- входящее MAX -> Telegram
- reply Telegram -> MAX
- медиа MAX -> Telegram
- reboot VM -> контейнер поднялся сам

Быстрый smoke-report:

```bash
cd /opt/maxtg-bridge
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

## 8. Обновления

> **Рекомендованный путь — Ansible:** `cd infra/ansible && ansible-playbook deploy.yml --check --diff && ansible-playbook deploy.yml`.
> Ручной workflow ниже — fallback на случай, если ansible недоступен.

```bash
cd /opt/maxtg-bridge
git pull
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml build
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml up -d
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=50
```

## 9. Если домашний IP сменился

1. Открыть Hetzner Cloud Firewall в панели.
2. Заменить старый source-IP для `22/tcp` на новый `x.x.x.x/32`.
3. Если доступ на сервер потерян, зайти через Hetzner Console / LISH.
4. На сервере обновить правило `UFW`:

```bash
sudo ufw delete allow from <OLD_IP> to any port 22 proto tcp
sudo ufw allow from <NEW_IP> to any port 22 proto tcp comment 'SSH from home IP'
sudo ufw status numbered
```

## 10. Recovery

Минимум для восстановления:

- `.env.secrets`
- `.env`
- `config.local.yaml`
- `data/max_bridge_session*`
- `data/bridge.db`

При компрометации:

1. удалить VM
2. поднять новую
3. восстановить файлы
4. поднять контейнер заново

## 11. Операционный чеклист

Как заходить на сервер:

```bash
ssh -i ~/.ssh/id_rsa deploy@<SERVER_IP>
```

Как проверить, что bridge жив:

```bash
cd /opt/maxtg-bridge
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml logs --tail=100 --since=10m
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

Как обновлять bridge:

```bash
cd /opt/maxtg-bridge
git pull
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml build
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml up -d
```
