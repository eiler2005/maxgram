# PyMax 2.5.0: изменения и совместимость bridge

Дата анализа: 2026-10-27. Обновление: `maxapi-python` 2.4.1 → 2.5.0.

Источники: [официальный релиз](https://github.com/MaxApiTeam/PyMax/releases/tag/v2.5.0),
[PR #111](https://github.com/MaxApiTeam/PyMax/pull/111),
[PyPI 2.5.0](https://pypi.org/project/maxapi-python/2.5.0/).
Релиз опубликован 2026-10-25. Проверен код wheel, а не только release notes;
SHA-256 wheel: `20dcf9bfe658fd75434cc370f12c6a0114d85ee109309789c19174710cb8f479`.

## Что изменилось

| Изменение upstream | Значение для bridge |
|---|---|
| FSM с TTL, DI по type annotations, опциональный Dishka, middleware | Новые средства разработки ботов. Bridge их не подключает; extra `dishka` не устанавливается. |
| Фильтры теперь наследуются от `Filter`, plain callable/lambda больше не поддерживаются | Bridge регистрирует callbacks без upstream filters, поэтому миграция фильтров не нужна. |
| Первый подходящий handler в каждом router, затем дочерние routers | В bridge один typed callback на тип события. Raw registration идемпотентна; её повторный вызов не создаёт второй callback. |
| `on_raw` выполняется в `finally`, в том числе при typed parse/handler failure | Сохраняется путь raw recovery для неполных или неизвестных payloads; проверен реальным dispatcher. |
| `InlineKeyboardAttachment.keyboard` — `InlineKeyboard`, строки в `.buttons`, типизированные кнопки, обязательный `callback_id` | Конвертация в существующий MAX DTO продолжает работать. Для строкового `OPEN_APP.web_app` исправлено сохранение исходной подписи кнопки; проверены legacy, `LINK` и `OPEN_APP`. |
| `ReactionUpdateEvent.message_id` теперь `int`; `reactionInfo` раскрывает counters, totalCount и yourReaction | Bridge преобразует id в строку и передаёт counters/total в прежний контракт. Собственная реакция не подменяет автора реакции. |
| `read_message(message_id)` принимает `int` | Bridge не вызывает этот метод; изменение не требует новых read receipts. |
| Сторис, комментарии каналов, `Message.post_id`, `send_callback()` | Upstream API расширился, но bridge не включает новые подписки/команды или нажатия bot callback. Comment-specific `send_at` ограничения не затрагивают существующий send path. |
| Android fingerprints обновлены до 26.37.1/build 6900 | Factory выбирает свежую версию/build через catalog и передаёт ту же app version в Client; persisted session не очищается. |
| `App.disconnect()` отделён от полного `close()`, App/FSM сохраняются при upstream reconnect, relogin ограничен одной попыткой | Решение ADR-004 сохранено: bridge использует one-shot `connect()`, ждёт закрытия connection и создаёт fresh client во внешнем reconnect loop. `reconnect`, `relogin`, `telemetry` выключены. |
| LZ4 flags выше 127 теперь разрешены; Zstandard остаётся на 0xFF | Bridge меняет только msgpack serializer, поэтому получает upstream исправление. Реальные LZ4 literal blocks проверены с flags 0x80/0xFE. |
| Исправлена загрузка фото с разными URL сервера | Используется через существующий PyMax media gateway; TLS verification и MAX CA context сохраняются. |
| Markdown сохраняет `_` внутри слов и literal inline/fenced code | Найдена дополнительная несовместимость URL renderer: он вставлял link markup внутрь кода. Теперь code spans защищены; внешние URL остаются кликабельными. |

Дополнительные исправления upstream: пробельный `Command`, лимит попыток пароля
при QR auth, сохранение типа config errors и cleanup при ошибках запуска.
Bridge сохраняет явный SMS/2FA operator flow и не вызывает QR reauth автоматически.

## Что изменено в репозитории

- Pin `maxapi-python==2.5.0`; минимум `aiohttp` поднят до `3.13.5` по metadata
  нового пакета, верхняя граница `<3.14` сохранена.
- `src/adapters/max/events.py` распознаёт строковые `web_app`/`webApp` как URL
  вместе с подписью кнопки; nested legacy forms продолжают работать.
- `backends/pymax/media.py` сохраняет literal inline/fenced code до форматирования.
- Обновлён runtime version pin и добавлены проверки реального dispatcher.
- Обновлены English/Russian README и architecture tour, architecture, context,
  tests, ADR-004 и operator guidance.

При анализе также устранены устаревшие operator notes: актуальный TCP sequence
имеет 16-bit range, а exact `MSG_GET` использует `messageIds: [message_id]`.

## Проверка и rollout

Полный gate — [docs/tests.md](tests.md): `pip check`, `compileall`, repo/scoped
Ruff, scoped mypy и pytest с coverage не ниже 75%. Новые dispatcher-тесты строят
runtime в `tmp_path`, не подключаются к MAX и не используют production credentials.
Тесты с fake Telegram подтверждают код доставки, но не заменяют живую переписку.

После commit/push: production backup, затем штатные Ansible preflight и deploy.
Release собирается из уже отправленного commit; env/state остаются приватными.
При повторной проверке сверяются версия установленного пакета, checksum файлов
в контейнере, startup self-tests и `/status` (`max_link`, `tg_link`, очереди).
Не запускать отдельный MAX-клиент рядом с bridge и не делать профилактический reauth.

При откате восстановить предыдущий immutable release и выполнить тот же build/up;
backup нужен для восстановления повреждённого state, а не вместо rollback кода.
