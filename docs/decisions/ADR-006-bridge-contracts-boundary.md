# ADR-006: Bridge contracts boundary для transport adapters

**Статус:** Принято  
**Дата:** 2026-05-22  
**Контекст:** Архитектурный рефакторинг зависимости от pymax/aiogram

## Проблема

`BridgeCore` должен содержать routing, dedup, topic management, commands и recovery registry без зависимости от конкретных транспортных библиотек. Фактически он импортировал `MaxMessage`, `MaxAttachment` и concrete adapter-типы из `src.adapters.max_adapter`, из-за чего доменные модели были привязаны к pymax adapter-слою.

Это мешало явно держать pymax-грабли внутри `MaxAdapter` и ослабляло обещанную архитектурную границу "core без зависимости от транспорта".

## Решение

Ввести `src/bridge/contracts.py` как единственный shared boundary между core и adapters:

- dataclass-модели: `MaxMessage`, `MaxAttachment`, `MaxAttachmentFailure`, `MaxIssue`, `MaxRecoverySnapshot`
- Protocol-порты: `MaxBridgePort`, `TelegramBridgePort`, `OpsNotifierPort`
- bridge-level helper-политики: `is_probable_client_cid`, `MAX_DM_SWEEP_BACKFILL_SECONDS`

`BridgeCore` импортирует только contracts и repository/config/runtime слои. Concrete adapters остаются в composition/bootstrap entrypoints (`src/main.py`, maintenance scripts) и в собственных adapter tests.

## Последствия

- Pymax-specific protocol hooks, reconnect details, media download quirks and lazy `pymax` imports остаются внутри `src/adapters/max_adapter.py`.
- Aiogram-specific bot/dispatcher logic остаётся внутри `src/adapters/tg_adapter.py`.
- Старые imports из `src.adapters.max_adapter` для shared MAX dataclass-моделей временно продолжают работать через re-export, но canonical import теперь `src.bridge.contracts`.
- Архитектурная граница защищена regression-тестами: `BridgeCore` не импортирует concrete adapters, а contracts не импортируют `pymax`, `aiogram` или adapter-слой.
