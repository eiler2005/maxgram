# Disaster Recovery Drill

Статус: `not yet executed` на 2026-05-25, потому что под рукой нет отдельной VPS
для безопасной проверки восстановления.

Цель drill: доказать, что backup набора `env/config/data` можно восстановить на
чистой машине без потери SQLite-состояния bridge, MAX session-файлов и runtime
health artifacts.

## Preconditions

- Не использовать production host для destructive-проверок.
- Не публиковать токены, телефоны, invite links, тексты сообщений, media или raw
  MAX payloads в issue/PR/log excerpts.
- На тестовой VPS должен быть доступ к тому же deploy playbook и зашифрованным
  secret material тем же способом, что в production.
- Перед drill зафиксировать backup artifact id, дату, размер и checksum внешнего
  архива, если backup-система его предоставляет.

## Checklist

1. Поднять чистую VPS с тем же базовым OS profile, что production.
2. Проверить, что на ней нет работающего bridge instance.
3. Развернуть код и зависимости обычным Ansible/deploy путём без запуска bridge.
4. Восстановить backup `env/config/data` в целевые пути.
5. Проверить права владельца и доступность SQLite:
   `sqlite3 data/bridge.db 'PRAGMA integrity_check;'`.
6. Запустить bridge в test/smoke режиме или обычным service start, если тестовая
   VPS не подключена к production Telegram/MAX окружению.
7. Проверить evidence:
   - service reaches `active/running`;
   - `data/health_heartbeat.json` обновляется;
   - `data/health_state.json` читается без JSON ошибок;
   - `data/maxtg_bridge.prom` создаётся, если metrics writer включён;
   - `schema_migrations` содержит версии `1,2,3`;
   - startup logs не содержат токены, телефоны, message text или raw payloads.
8. Остановить тестовый bridge и удалить тестовую VPS.

## Expected Evidence

- Drill date and operator.
- Backup artifact id.
- Git commit / image tag.
- `PRAGMA integrity_check` result.
- Last heartbeat timestamp.
- Sanitized service status excerpt.
- Sanitized list of restored files/directories, без secret values.

## Current Gap

Пока drill не выполнен, production recovery остаётся документированным, но не
измеренным. Не указывать RTO/RPO как факт до первой полной проверки на отдельной
VPS.
