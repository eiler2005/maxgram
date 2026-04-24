# Ansible для MAX→Telegram Bridge

Автоматизация runbook'ов, описанных в [docs/runbooks/hetzner-production.md](../../docs/runbooks/hetzner-production.md). Дизайн — в [docs/reviews/ansible-handoff.md](../../docs/reviews/ansible-handoff.md).

## Quickstart

### Первый запуск с локальной машины

```bash
# 1. Установить ansible
brew install ansible            # macOS
# или: pipx install ansible-core

# 2. Скопировать inventory из примера и вписать реальный IP
cp infra/ansible/inventory/production.ini.example infra/ansible/inventory/production.ini
$EDITOR infra/ansible/inventory/production.ini

# 3. Создать vault password (если будут зашифрованные vars)
echo 'your-vault-password' > infra/ansible/.vault_pass
chmod 600 infra/ansible/.vault_pass

# 4. Проверить SSH-доступ
ansible -i infra/ansible/inventory/production.ini bridge_servers -m ping
```

### Регулярный deploy

```bash
cd infra/ansible

# Сначала всегда dry-run
ansible-playbook deploy.yml --check --diff

# Затем (после Hetzner snapshot, если важное изменение) — реальный
ansible-playbook deploy.yml
```

## Playbook'и

| Playbook | Когда запускать | Цель |
|----------|----------------|------|
| `deploy.yml` | На каждый apt-обновление кода | rsync кода + `docker compose build/up -d` + healthcheck |
| `backup.yml` | Перед любым рискованным изменением | `tar` бэкап `data/` + envs, скачать на ноут |
| `recover.yml` | Только в аварии на пустом VM | Развернуть бэкап на свежем сервере |
| `bootstrap.yml` | **Только для нового VM** | Юзер `deploy`, базовые пакеты, Docker |
| `hardening.yml` | **Только для нового VM, в связке с bootstrap** | sshd, UFW, fail2ban, unattended-upgrades |

`bootstrap.yml` и `hardening.yml` **никогда не применяются к текущему prod** — он уже захардёнен по runbook'у руками.

## Test lab (без отдельных мощностей)

Bootstrap и hardening отлаживаются в одноразовом docker-контейнере с systemd:

```bash
# Поднять lab
docker run -d --rm --name ansible-lab \
  --privileged \
  -p 2222:22 \
  geerlingguy/docker-ubuntu2404-ansible \
  /lib/systemd/systemd

# Положить SSH-ключ
docker exec -i ansible-lab bash -c \
  'mkdir -p /root/.ssh && cat >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys' \
  < ~/.ssh/id_rsa.pub

# Скопировать inventory/lab.ini.example в inventory/lab.ini (создать пример при необходимости)
# Прогнать
ansible-playbook -i inventory/lab.ini bootstrap.yml hardening.yml \
  --skip-tags ufw,unattended

# Проверить, что SSH не сломан
ssh -p 2222 -i ~/.ssh/id_rsa root@127.0.0.1 systemctl status ssh

# Прибить
docker rm -f ansible-lab
```

В docker UFW и unattended-upgrades не работают (kernel modules / dpkg-config), поэтому пропускаем по тегам.

## Что НЕ автоматизировано

- Создание Hetzner VM (делается через панель).
- Cloud Firewall в Hetzner панели (отдельный API token).
- SMS reauth для MAX (только владелец с телефоном).
- Содержимое `.env.secrets` / `.env` / `config.local.yaml` — копируется на сервер вручную через `scp`.
- Auto-deploy по push в git — намеренно не делаем (запрет в [CLAUDE.md](../../CLAUDE.md)).

## Verification после deploy

```bash
ssh deploy@<SERVER_IP> 'cd /opt/maxtg-bridge && \
  docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps && \
  python3 scripts/smoke_check.py --db data/bridge.db --minutes 15'
```

Дополнительно — отправить тестовое сообщение в MAX и убедиться, что оно пришло в Telegram (см. [docs/runbooks/operations.md](../../docs/runbooks/operations.md), раздел "Базовая живая smoke-проверка").
