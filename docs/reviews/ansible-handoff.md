# Ansible Handoff для MAX→Telegram Bridge

## Executive Summary

- Текущий production-рантайм: один `Ubuntu 24.04` VM на Hetzner Cloud.
- Деплой приложения идёт через `Docker Compose` в `<PROJECT_ROOT>` на сервере.
- Основной процесс приложения запускается как `python -m src.main`.
- Внутри контейнера уже реализован supervisor/runtime shell: контейнер должен оставаться `Up`, даже если MAX/TG-интеграция деградировала.
- Runtime health уже персистится в `data/health_state.json`, `data/health_events.jsonl`, `data/alert_outbox.jsonl`, `data/health_heartbeat.json`.
- Секреты уже разделены: non-secret env в `.env`, чувствительные данные в `.env.secrets`, оба файла не должны попадать в git.
- Основной ops-канал сейчас: owner DM через Telegram-бота; дополнительный forum-topic fanout опционален.
- Recovery и deploy сейчас уже документированы, но всё ещё partly manual: sync/rebuild/up/check, backup и ручное восстановление сессии.
- Цель Ansible для этого проекта: автоматизировать инфраструктурные и деплойные шаги без изменения логики bridge.

## Зачем здесь Ansible

Ansible нужен не для логики приложения, а для снижения операционного риска:

- воспроизводимо поднимать новый сервер с нуля;
- одинаково выполнять deploy на уже живой production VM;
- уменьшить drift между runbook и реальным сервером;
- ускорить backup/recovery после аварии;
- сделать hardening, Docker setup и app rollout процедурой, а не набором ручных команд.

Ключевая цель для v1:

- безопасно встроить Ansible в уже существующий `Hetzner + Docker Compose` production без предположения, что сервер можно пересоздать.

## Current Production Shape

### High-level shape

| Компонент | Текущее состояние |
|-----------|-------------------|
| Infra | Один Hetzner VM |
| OS | `Ubuntu 24.04` |
| App deploy | `Docker Compose` |
| App root | `<PROJECT_ROOT>` |
| Entrypoint | `python -m src.main` |
| Persistent state | `data/` |
| Health model | persisted runtime health files |
| Alerts | owner DM, optional ops topic |
| Secrets | вне git |

### Что реально живёт на сервере

- рабочая директория приложения: `<PROJECT_ROOT>`
- compose-файлы и app code лежат рядом с конфигами
- persistent state живёт в `data/`
- локальные приватные файлы:
  - `.env`
  - `.env.secrets`
  - `.env.host`
  - `config.local.yaml`
  - `data/`

### Persistent state и runtime artifacts

| Путь | Назначение |
|------|------------|
| `data/bridge.db` | SQLite state bridge |
| `data/max_bridge_session*` / `data/session.db` | MAX session state |
| `data/health_state.json` | текущий health snapshot |
| `data/health_events.jsonl` | история health transitions |
| `data/alert_outbox.jsonl` | неотправленные системные алерты |
| `data/health_heartbeat.json` | heartbeat для Docker healthcheck |

### Текущий deploy shape

Текущая процедура на production концептуально такая:

1. синк кода в `<PROJECT_ROOT>`
2. проверка/сохранение локальных приватных файлов
3. `docker compose build`
4. `docker compose up -d`
5. post-deploy checks:
   - `docker compose ps`
   - Docker healthcheck
   - application logs
   - smoke check по SQLite

## Operational Constraints

### Что обязательно учитывать

- Нельзя ломать живой production server.
- Нельзя предполагать, что VM будет пересоздана перед внедрением Ansible.
- Нельзя коммитить реальные production secrets в git.
- Нельзя автоматизировать SMS `MAX reauth`.
- Docker может уже быть установлен и должен поддерживаться мягко.
- Bootstrap для новых серверов и deploy для существующего сервера должны быть разными сценариями.

### Дополнительные ограничения

- Нельзя считать `data/` одноразовым артефактом: там живут SQLite state и MAX session.
- Нельзя по умолчанию применять опасные сетевые изменения на уже живом хосте.
- Нельзя смешивать hardening и обычный app deploy в один обязательный playbook.
- Нельзя ломать текущий split `.env` / `.env.secrets`.

## Что Ansible должен взять на себя

### Should Own

| Область | Что должен делать Ansible |
|---------|---------------------------|
| Bootstrap новых VM | базовая подготовка сервера |
| Docker layer | установка или мягкая проверка Docker и compose plugin |
| App deploy | выкладка кода/релизного бандла и запуск compose |
| Config layout | создание директорий, права, env/template раскладка |
| Runtime verification | `docker compose ps`, healthcheck, logs, smoke check |
| Backup/recovery | orchestration backup и восстановление из backup |
| Optional hardening | отдельный playbook для ssh/ufw/fail2ban baseline |

### Must Not Own

| Область | Почему вне зоны Ansible |
|---------|-------------------------|
| Логика bridge | это код приложения, а не infra automation |
| Runtime health логика | уже реализована внутри приложения |
| MAX SMS reauth | требует ручного шага владельца |
| SQLite state как код | это production data, а не шаблон |
| Открытое хранение боевых секретов | противоречит secret policy |
| Агрессивные сетевые изменения по умолчанию | риск сломать доступ к существующему серверу |

## Recommended v1 Scope

### Приоритет для существующего production

Первый безопасный scope:

- `deploy.yml` для уже живого production-сервера
- `backup.yml`
- `recover.yml`

### Отдельные сценарии

- `bootstrap.yml` только для новых VM
- `hardening.yml` отдельно и не как обязательная часть каждого deploy

### Что должен уметь v1 deploy

- убедиться, что `<PROJECT_ROOT>` существует;
- убедиться, что Docker service доступен;
- разложить non-secret templates и app files;
- не перетирать `.env.secrets`, `config.local.yaml`, `data/`;
- выполнить `docker compose build && docker compose up -d`;
- проверить, что контейнер вышел в `healthy`;
- дать понятный провал, если healthcheck или smoke-check не сошлись.

## Proposed Ansible Layout

Рекомендуемая структура:

```text
infra/ansible/
├── inventory/
│   └── production.ini
├── group_vars/
│   └── production.yml
├── site.yml
├── bootstrap.yml
├── deploy.yml
├── backup.yml
├── recover.yml
└── roles/
    ├── base/
    ├── docker/
    └── bridge_app/
```

### Role Responsibilities

| Role | Ответственность |
|------|------------------|
| `base` | пользователь, базовые пакеты, директории, optional ssh baseline |
| `docker` | установка или проверка Docker, compose plugin, сервис `docker` |
| `bridge_app` | раскладка app/config/env templates, sync release bundle, `docker compose build/up -d`, health verification |

## Secrets Policy

### Что допустимо хранить в git

- inventory structure
- role defaults
- non-secret vars
- шаблоны файлов с placeholders

### Что нельзя хранить открыто в git

- содержимое `.env.secrets`
- любые реальные production identifiers
- owner ids, forum ids, phone numbers, tokens
- SSH private keys

### Допустимые варианты secret management

| Вариант | Статус для review |
|---------|-------------------|
| `ansible-vault` | допустимый вариант |
| `sops` | допустимый вариант |
| полностью вне ansible repo | допустимый v1-компромисс |

Рекомендуемая политика для v1:

- не навязывать сразу конкретный secret backend;
- зафиксировать, что `.env.secrets` никогда не хранится открыто в git;
- разрешить либо `vault/sops`, либо ручную загрузку secrets на сервер как временный этап.

## Safe Rollout Strategy

### Порядок внедрения

1. Сначала только `deploy.yml` на существующем сервере.
2. Перед первым реальным применением обязательны `--check --diff`.
3. Перед чувствительными шагами обязателен backup:
   - `.env`
   - `.env.secrets`
   - `config.local.yaml`
   - `data/`
4. `hardening.yml` не должен быть частью каждого deploy.
5. `bootstrap.yml` использовать только для новых VM.

### Post-deploy validation

После deploy Ansible должен проверить:

- `docker compose ps`
- Docker healthcheck
- свежие application logs
- smoke check по `bridge.db`

Минимальный пример проверок:

```bash
docker compose --env-file .env.host -f deploy/docker-compose.prod.yml ps
docker inspect --format '{{json .State.Health}}' <CONTAINER_NAME>
python3 scripts/smoke_check.py --db data/bridge.db --minutes 15
```

## Open Decisions for Reviewers

Ниже решения, которые можно оставить на Ansible review:

| Вопрос | Варианты |
|--------|----------|
| Docker orchestration | Ansible Docker modules vs `docker compose` через `command` |
| Secrets handling | `ansible-vault` vs `sops` vs manual secret provisioning |
| Release transport | `rsync`-based deploy vs `git checkout/pull` на сервере |
| Hetzner automation v2 | нужен ли позже Cloud Firewall/API automation |

Рекомендуемый default для v1:

- использовать существующий Docker мягко;
- не переустраивать живой сервер агрессивно;
- deploy делать максимально близко к текущему ручному процессу.

## Acceptance Criteria для будущей Ansible-работы

- существующий production server можно обновить без ручной правки app-файлов;
- новый сервер можно поднять повторяемо;
- deploy не ломает `.env.secrets`, `config.local.yaml`, `data/`;
- контейнер после deploy выходит в `healthy`;
- rollback/recovery path описан и воспроизводим;
- документация и playbooks не содержат реальных production identifiers.

## Источники правды в текущем репозитории

Использовать как source-of-truth:

- [Hetzner Production Runbook](../runbooks/hetzner-production.md)
- [Operations Runbook](../runbooks/operations.md)
- [Architecture](../architecture.md)

Что из них брать:

| Источник | Что извлекать |
|----------|---------------|
| `docs/runbooks/hetzner-production.md` | deploy shape, backup/recovery expectations, server baseline |
| `docs/runbooks/operations.md` | operational constraints, health checks, failure handling, validation steps |
| `docs/architecture.md` | runtime health model, supervisor shell, secret layout, app boundaries |

## Sanitization Rules

Этот документ специально подготовлен для review другими агентами и должен оставаться sanitized.

Нельзя включать:

- реальный server IP
- реальный hostname
- реальные usernames/owner ids/forum ids
- phone numbers
- bot tokens
- персональные имена из chat bindings

Нужно использовать:

- `<SERVER_IP>`
- `<DEPLOY_USER>`
- `<PROJECT_ROOT>`
- `<CONTAINER_NAME>`
- placeholders вместо всех production identifiers

## Итог для implementer/reviewer

Если агент берёт этот handoff в работу, его стартовая задача должна звучать так:

> Спроектировать и затем реализовать минимальный Ansible-layer для существующего MAX→Telegram Bridge production, не меняя приложение, не трогая ручной SMS reauth, не раскрывая secrets и не предполагая пересоздание текущего Hetzner VM. Первый безопасный фокус — `deploy.yml` для текущего сервера, затем `bootstrap.yml`, `backup.yml`, `recover.yml`, с отдельным `hardening.yml`.
