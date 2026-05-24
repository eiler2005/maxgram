# ADR-004: Стратегия reconnect для pymax (fresh client)

**Статус:** Принято  
**Дата:** 2026-04  
**Обновлено:** 2026-05-24 для PyMax 2.0.1
**Контекст:** Обнаружен баг при отладке production

## Проблема

`SocketMaxClient(reconnect=True)` имеет баг: при каждом reconnect `client.chats` и `client.dialogs`
пополняются без очистки. После 5+ reconnect: 12 dialogs → 408 dialogs → OOM crash.

Дополнительно: `send_fake_telemetry=True` (default) отправляет LOG-опкод сразу после подключения,
что вызывает `SSL: TLSV1_ALERT_RECORD_OVERFLOW` → мгновенный disconnect → reconnect storm.

## Варианты

1. `reconnect=True` — встроенный reconnect pymax → OOM баг
2. `reconnect=False` + outer loop с тем же клиентом → кеш всё равно накапливается
3. `reconnect=False` + outer loop с `_make_client()` (fresh instance) → чистый кеш ✅

## Решение

```python
async def start(self):
    while True:
        self._client = await self._make_client()   # fresh каждый раз
        self._client.on_start(_on_start)
        self._client.on_message()(self._handle_raw_message)
        await self._client.start()                  # блокирует до disconnect
        self._started = False
        await asyncio.sleep(retry_delay)

async def _make_client(self):
    return Client(
        phone=self._phone,
        work_dir=self._data_dir,
        session_name=self._session_name,
        extra_config=ExtraConfig(
            reconnect=False,          # ← управляем сами
            telemetry=False,          # ← не включаем telemetry
        ),
    )
```

## Последствия

- Чистый кеш при каждом reconnect — нет OOM
- Нет SSL storm
- Кеш пользователей/чатов пересобирается при каждом reconnect (приемлемо)
- `on_start` handlers вызываются при каждом reconnect — нужен флаг `_started_once` для дедупликации уведомлений
- В PyMax 2 встроенный ping loop используется вместо bridge private ping patch; `failfast_ping_config()` для PyMax 2 возвращает `None`.
- Readiness не равен только `_started`: после network/router flap PyMax 2 может закрыть внутренний transport, но не вернуть управление из `start()`. Поэтому `MaxAdapter.is_ready()` проверяет `client.is_connected`; watchdog видит закрытый transport и после grace-period может выполнить self-heal restart процесса.
