# План реализации миграции на PyMax 2.0.1

Дата: 2026-05-24

Статус: implementation spec для отдельной миграционной работы. Этот документ
не меняет runtime-код. Следующий агент или инженер должен использовать его как
готовый handoff-план и сначала получить решение владельца проекта о переходе.

## Короткое решение

Мигрируем MAX backend с `maxapi-python==1.2.5` на
`maxapi-python==2.0.1`, но не меняем архитектуру bridge кардинально.

Фиксируем решения:

- Две версии pymax одновременно не поддерживаем.
- Текущий backend `src/adapters/max/backends/pymax/` переписываем под PyMax
  2.0.1.
- `BridgeCore`, Telegram adapter, DB schema и operation services не трогаем
  без отдельной технической причины.
- Лучшие части PyMax 2 используем внутри backend boundary, а не протаскиваем
  pymax-объекты в core или service layer.
- Rollback: git rollback миграционной ветки плюс возврат зависимости на
  `maxapi-python==1.2.5`.
- Git push и production deploy запрещены без явной команды владельца проекта.

## Preflight перед разработкой

Эти шаги обязательны перед началом реализации миграции. Они относятся именно к
кодовой миграции, а не к написанию этого документа.

1. Создать отдельную ветку:

   ```bash
   git checkout -b migration/pymax-2
   ```

   Если такая ветка уже существует, выбрать понятное имя вроде
   `migration/pymax-2-2026-05-24`.

2. Проверить рабочее дерево:

   ```bash
   git status --short
   ```

   Перед правками нужно понять происхождение всех незакоммиченных изменений.
   Чужие изменения не откатывать и не переписывать. Если они пересекаются с
   миграцией, сначала согласовать порядок действий.

3. Зафиксировать rollback path:

   - вернуть код backend adapter к состоянию до миграции через git rollback;
   - вернуть `requirements.txt` с `maxapi-python==2.0.1` на
     `maxapi-python==1.2.5`;
   - восстановить session DB из backup, если live-run PyMax 2 изменил формат
     или состояние сессии.

4. Перед первым live-run сделать backup MAX session DB.

   Session DB находится в рабочей директории pymax, которую проект передаёт в
   `work_dir` и `session_name`. Перед запуском новой версии сохранить копию
   соответствующего файла из `data/` или другого настроенного `DATA_DIR`.

5. Не делать production deploy, restart production service или `git push` без
   отдельной явной команды.

6. Проверить, что runtime Python в целевой среде соответствует требованию
   PyMax 2:

   ```bash
   python --version
   ```

   `maxapi-python==2.0.1` требует Python `>=3.10`.

## Цель миграции

Перевести MAX transport implementation на PyMax 2.0.1 и сохранить текущую
семантику bridge:

- MAX -> Telegram forwarding остается транспортно-нейтральным за пределами
  MAX adapter.
- Telegram topics, reply routing, recovery registry и idempotency не меняются.
- `pymax` imports остаются только внутри
  `src/adapters/max/backends/pymax/`.
- Текст сообщений, raw payloads, invite links, токены, телефоны, proxy
  credentials и media bytes не попадают в logs или SQLite.
- MAX egress продолжает управляться только MAX adapter layer.
- Внешний reconnect loop проекта остается главным механизмом восстановления.

Успешная миграция должна быть незаметна для `BridgeCore`, Telegram adapter и
operation services.

## Архитектурное решение

Текущая архитектура уже содержит правильную точку миграции:

```text
MaxAdapter facade
  -> operation services
     -> MaxClientPort DTO/protocol in src/adapters/max/ports.py
        -> PymaxClientAdapter
           -> raw PyMax client
```

Рекомендуемое решение: оставить эту архитектуру и заменить только реализацию
backend boundary.

Что меняется:

- `PymaxBackend` создает PyMax 2 `Client`.
- `PymaxClientAdapter` переводит PyMax 2 events, models и raw frames в текущие
  DTO и callbacks bridge.
- Тесты backend boundary обновляются под PyMax 2 API.

Что не меняется:

- `src/bridge/core.py` и `src/bridge/contracts.py`;
- Telegram adapter;
- DB schema;
- operation services в `src/adapters/max/*`;
- recovery registry и routing semantics;
- production deploy process.

Почему не делаем кардинальный rewrite:

- Проект является privacy-sensitive bridge, а не обычным PyMax bot.
- ADR-007 уже изолирует pymax внутри backend implementation.
- PyMax 2 domain objects удобны, но их протаскивание в services усложнит
  будущую замену MAX library.
- Небольшой backend-only migration имеет меньший blast radius и проще
  откатывается.

Итог: архитектуру приложения кардинально менять не нужно. Нужно аккуратно
переписать PyMax backend и сохранить существующие adapter contracts.

## Что используем из PyMax 2

В первой миграции используем только то, что помогает заменить старую library
без расширения границ зависимости.

### `Client + ExtraConfig`

PyMax 1:

```python
from pymax import SocketMaxClient

client = SocketMaxClient(
    phone=phone,
    work_dir=data_dir,
    session_name=session_name,
    reconnect=False,
    send_fake_telemetry=False,
)
```

PyMax 2:

```python
from pymax import Client, ExtraConfig

client = Client(
    phone=phone,
    work_dir=data_dir,
    session_name=session_name,
    extra_config=ExtraConfig(
        reconnect=False,
        telemetry=False,
    ),
)
```

Обязательные флаги:

- `reconnect=False`: внутренний reconnect pymax не используем, чтобы внешний
  supervisor/reconnect loop проекта создавал свежий client.
- `telemetry=False`: сохраняем текущую safety posture и не включаем
  pymax-telemetry по умолчанию.

### Native `on_raw`

PyMax 2 имеет публичный raw event hook `client.on_raw()`. Это лучше старого
private patch вокруг `_handle_message_notifications`.

Raw frame должен быть преобразован в текущую форму bridge:

```python
{
    "opcode": frame.opcode,
    "cmd": frame.cmd,
    "seq": frame.seq,
    "payload": frame.payload,
}
```

Это позволяет оставить `src/adapters/max/events.py`,
`src/adapters/max/raw/history.py` и media recovery без переписывания.

### Pydantic v2 models

PyMax 2 domain models используют Pydantic v2 и aliases. Для raw message payload
использовать:

```python
Message.model_validate(payload)
```

Старый `Message.from_dict(...)` больше не использовать.

### `pymax.protocol.Opcode`

Старые imports из `pymax.static.enum` заменить на:

```python
from pymax.protocol import Opcode
```

Нужные opcodes:

- `Opcode.CHAT_HISTORY`;
- `Opcode.FILE_DOWNLOAD`;
- `Opcode.VIDEO_PLAY`;
- `Opcode.AUDIO_PLAY`;
- `Opcode.PING`.

### Cleaner transport layer для MAX egress

PyMax 2 разделяет client, app, connection manager и transport. Это полезно для
аккуратного сохранения текущего MAX-only egress.

Предпочтение: custom PyMax 2 transport/client внутри
`src/adapters/max/backends/pymax/`, который использует существующий
`MaxEgressProfile.socket_connector`.

Причина: текущий `MaxEgressProfile` уже решает privacy и redaction задачи,
а также сохраняет fail-closed поведение Channel M.

### Built-in ping

PyMax 2 имеет встроенный ping loop. В первой миграции используем его как
основной механизм и не переносим прежний private ping patch.

`MaxClientPort.install_interactive_ping(...)` можно сохранить как no-op для
PyMax 2, чтобы не расширять изменения за пределы backend boundary.

## Что не используем в первой миграции

Сознательно не используем эти возможности PyMax 2 на первом этапе:

- Не переносим `message.answer(...)` в operation services.
- Не переносим `chat.history(...)` в operation services.
- Не переносим `user.add_contact(...)` в operation services.
- Не строим bridge вокруг PyMax routers.
- Не поддерживаем v1/v2 adapter одновременно.
- Не меняем `BridgeCore`.
- Не меняем Telegram adapter.
- Не меняем DB schema.
- Не меняем operation services без доказанной необходимости.
- Не включаем production deploy как часть миграции.

PyMax routers и active domain methods можно рассмотреть позже, но только если
они останутся внутри backend boundary или будут обернуты transport-neutral
ports.

## Пошаговый порядок реализации

### Step 1: Dependency

Файл:

```text
requirements.txt
```

Заменить:

```text
maxapi-python==1.2.5
```

на:

```text
maxapi-python==2.0.1
```

Проверить Python:

```bash
python --version
```

Требование PyMax 2: Python `>=3.10`.

После изменения зависимости установить ее только в локальном venv или в
отдельной тестовой среде. Production окружение не обновлять до отдельного
решения.

### Step 2: `PymaxBackend`

Файл:

```text
src/adapters/max/backends/pymax/backend.py
```

Основные изменения:

- заменить `SocketMaxClient` на `Client`;
- создать `ExtraConfig(reconnect=False, telemetry=False)`;
- заменить imports на PyMax 2 modules;
- заменить old payload classes на PyMax 2 payload classes;
- заменить `Message.from_dict(...)` на `Message.model_validate(...)`;
- сохранить egress через текущий `MaxEgressProfile.socket_connector`;
- не выносить pymax imports за пределы backend package.

Старые imports убрать:

```python
from pymax import SocketMaxClient
from pymax.exceptions import SocketNotConnectedError
from pymax.static.constant import DEFAULT_PING_INTERVAL
from pymax.static.enum import Opcode
from pymax.payloads import UserAgentPayload
```

Новые imports ориентировочно:

```python
from pymax import Client, ExtraConfig, File, Photo, Video
from pymax.api.messages.payloads import ChatHistoryPayload, GetVideoPayload
from pymax.protocol import Opcode
from pymax.types import Message
```

Фактические import paths проверить по установленной версии
`maxapi-python==2.0.1`, но `pymax.protocol.Opcode` зафиксирован как целевой
источник opcodes.

#### `create_raw_client(...)`

`PymaxBackend.create_raw_client(...)` должен создавать PyMax 2 client с
обязательным config:

```python
extra_config = ExtraConfig(
    reconnect=False,
    telemetry=False,
)
```

Direct egress:

```python
client = Client(
    phone=phone,
    session_name=session_name,
    work_dir=work_dir,
    extra_config=extra_config,
)
```

Proxy/Channel M egress:

- использовать custom PyMax 2 client/transport внутри backend;
- не передавать raw proxy URL в места, где он может попасть в logs;
- сохранить redaction и fail-closed semantics текущего
  `src/adapters/max/network/egress.py`.

#### Preferred egress design

Предпочтительный вариант: custom transport на базе PyMax 2 `TCPTransport` или
custom `Client._build_connection()`.

Цель:

- `MaxEgressProfile.socket_connector.connect(host, port, timeout=...)`
  остается единственным способом открыть MAX socket через выбранный egress;
- TLS и PyMax protocol остаются в PyMax 2 transport stack;
- credentials не логируются;
- при недоступности Channel M нет автоматического fallback на direct egress.

Ориентировочная форма:

```python
class EgressTCPTransport(TCPTransport):
    def __init__(self, *, socket_connector, host, port, use_ssl=True):
        super().__init__(host=host, port=port, proxy=None, use_ssl=use_ssl)
        self._maxtg_socket_connector = socket_connector

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        raw_sock = await loop.run_in_executor(
            None,
            lambda: self._maxtg_socket_connector.connect(
                self._host,
                self._port,
                timeout=20.0,
            ),
        )
        self._reader, self._writer = await asyncio.open_connection(
            sock=raw_sock,
            ssl=self._use_ssl,
        )
```

Если реализация вместо этого использует `ExtraConfig.proxy`, сначала добавить
тесты, которые доказывают:

- proxy credentials не попадают в logs/errors;
- Channel M остается fail-closed;
- `MaxEgressProfile` не теряет контроль над MAX-only egress.

#### Payload helpers

`make_message_from_dict(...)`:

```python
return Message.model_validate(payload)
```

`fetch_history_payload(...)`:

```python
return ChatHistoryPayload(
    chat_id=chat_id,
    from_=from_time,
    forward=forward,
    backward=backward,
).to_payload()
```

`get_video_payload(...)`:

```python
return GetVideoPayload(
    chat_id=chat_id,
    message_id=message_id,
    video_id=video_id,
).to_payload()
```

Если PyMax 2 payload class использует другое имя поля, адаптировать только
внутри `backend.py` и покрыть unit test.

#### Ping compatibility

На первом этапе не переносить старый private fail-fast ping patch.

Рекомендуемое поведение:

- `PymaxBackend.failfast_ping_config()` может вернуть совместимый dict, если
  его ожидает текущий код lifecycle;
- `PymaxClientAdapter.install_interactive_ping(...)` для PyMax 2 делает no-op;
- встроенный PyMax 2 ping остается основным механизмом.

Более чистый follow-up после live stability: разрешить
`failfast_ping_config() -> dict[str, object] | None` и вернуть `None` для PyMax
2.

### Step 3: `PymaxClientAdapter`

Файл:

```text
src/adapters/max/backends/pymax/client_adapter.py
```

Задача adapter: сохранить текущий `MaxClientPort`, но внутри адаптировать
PyMax 2 signatures и models.

#### Connection state

PyMax 2 не обязан иметь старый `is_connected`. Использовать состояние
connection/app:

```python
is_open = bool(getattr(client._connection, "is_open", False))
```

или эквивалент через `client._app.connection.is_open`, если это фактический
путь в установленной версии.

Private access допустим только внутри backend adapter.

#### Startup wrapping

Старый код мог оборачивать private startup methods. В PyMax 2 private lifecycle
другой, поэтому первый migration pass должен оборачивать `client.start`.

Ориентировочно:

```python
def prepare_startup(self, error_handler):
    original_start = self._client.start

    async def wrapped_start(*args, **kwargs):
        try:
            return await original_start(*args, **kwargs)
        except Exception as exc:
            await error_handler(exc)
            raise

    self._client.start = wrapped_start
```

Цель: сохранить runtime issue reporting без привязки к большому набору private
methods PyMax 2.

#### Handlers PyMax 2

PyMax 2 callbacks принимают `(event, client)`, а текущие bridge callbacks
ожидают старую форму.

Message handler:

```python
def _wrap_message_handler(self, handler):
    async def wrapped(message, _client):
        return await handler(MaxClientMessage.from_object(message))

    return wrapped
```

Start handler:

```python
def register_start_handler(self, handler):
    async def wrapped(_client):
        result = handler()
        if inspect.isawaitable(result):
            await result

    self._client.on_start()(wrapped)
```

Регистрация:

```python
self._client.on_message()(wrapped_message_handler)
self._client.on_message_edit()(wrapped_edit_handler)
self._client.on_message_delete()(wrapped_delete_handler)
```

#### Native raw handler

Использовать `client.on_raw()`, а не private patch.

```python
def install_raw_message_interceptor(self, handler):
    if self._raw_handler_registered:
        return MaxRawInterceptorResult(installed=True, raw_handler_count=1)

    async def wrapped(frame, _client):
        await handler(
            {
                "opcode": frame.opcode,
                "cmd": frame.cmd,
                "seq": frame.seq,
                "payload": frame.payload,
            }
        )

    self._client.on_raw()(wrapped)
    self._raw_handler_registered = True
    return MaxRawInterceptorResult(installed=True, raw_handler_count=1)
```

`register_raw_receive_handler(...)` должен избегать двойной регистрации, если
raw interceptor уже установлен.

#### Raw requests

В PyMax 2 нет публичного `client.invoke(...)`. Для raw requests использовать
private `client._app.invoke(...)`, но только внутри `PymaxClientAdapter`.

```python
response = await self._client._app.invoke(
    opcode=opcode,
    payload=payload,
    cmd=Command.REQUEST,
    timeout=timeout,
)
```

Adapter должен вернуть dict с ключом `payload`, потому что текущие media/raw
services ожидают именно такую форму.

Если response является `InboundFrame`, нормализовать:

```python
return {
    "opcode": response.opcode,
    "cmd": response.cmd,
    "seq": response.seq,
    "payload": response.payload,
}
```

#### Sending media

Старый API:

```python
await client.send_message(..., attachment=attachment)
```

Новый API:

```python
await client.send_message(..., attachments=[attachment])
```

Правила:

- `media_type == "photo"` -> `Photo(path=...)`;
- `media_type == "video"` -> `Video(path=...)`;
- все остальные outbound media -> `File(path=...)`;
- если attachment нет, передавать `attachments=None` или не передавать
  аргумент, в зависимости от фактической signature PyMax 2.

Return contract:

```python
MaxSendResult(
    message_id=<id from PyMax result>,
    raw=<raw PyMax result or normalized dict>,
)
```

#### Own user id

В PyMax 2 current profile содержит `contact`.

```python
own_id = client.me.contact.id
```

Старые fallback paths через `client.me.id` не считать основным способом.

#### Snapshots

Сохранить текущие DTO:

- `MaxUserView`;
- `MaxChatView`;
- `MaxDialogView`;
- `MaxClientMessage`.

Mapping:

```python
contacts = client.contacts
users = client.users
chats = client.chats
```

`client._users` не использовать.

PyMax 2 может хранить dialogs, groups и channels в `client.chats`, поэтому
фильтровать по `Chat.type`.

Ориентировочный helper:

```python
def _chat_type_name(chat):
    value = getattr(chat, "type", None)
    value = getattr(value, "value", value)
    return str(value).upper()
```

Фильтры:

```python
dialogs = [chat for chat in client.chats or [] if _chat_type_name(chat) == "DIALOG"]
groups = [chat for chat in client.chats or [] if _chat_type_name(chat) == "CHAT"]
channels = [chat for chat in client.chats or [] if _chat_type_name(chat) == "CHANNEL"]
```

Live validation обязательно должна проверить случай own-initiated DM, где
`chat_id` может совпасть с own user id.

#### File, video, history helpers

`file_url(...)`:

- использовать `client.get_file_by_id(...)`, если он возвращает подходящий URL;
- не логировать raw file response целиком.

`video_payload(...)`:

- в первой миграции предпочтительно оставить raw `VIDEO_PLAY` через
  `raw_request(...)`, потому что текущий код уже умеет разбирать сложные
  nested payloads.

`raw_history_payload(...)`:

- оставить raw `CHAT_HISTORY` через `raw_request(...)`, чтобы recovery cache
  получил исходный server payload.

`history_messages(...)`:

- можно использовать public `client.fetch_history(...)`, если результат
  конвертируется в текущие `MaxClientMessage`.

### Step 4: Сохранить service boundary

После переписывания backend проверить, что pymax не появился вне:

```text
src/adapters/max/backends/pymax/
```

Operation services должны продолжать работать через `MaxClientPort`. Если PyMax
2 предлагает удобный метод, но он нужен service layer, добавить обертку в
`PymaxClientAdapter`, а не импортировать pymax в service.

Не менять:

- `BridgeCore`;
- `src/bridge/contracts.py`;
- Telegram adapter;
- DB schema;
- repository layer;
- recovery registry schema.

Исключение: изменение допустимо только если PyMax 2 делает текущий contract
невозможным. В этом случае сначала зафиксировать причину в миграционной ветке и
обновить tests/ADR.

### Step 5: Tests

Обновить и добавить unit tests до live validation.

Обязательные проверки:

- `ExtraConfig.reconnect is False`;
- `ExtraConfig.telemetry is False`;
- PyMax 2 message callback получает `(message, client)`, а bridge handler
  получает один `MaxClientMessage`;
- start callback получает `(client)`, а bridge start handler вызывается без
  аргументов;
- raw callback получает `InboundFrame`, а bridge raw handler получает dict с
  `opcode`, `cmd`, `seq`, `payload`;
- raw request вызывает `client._app.invoke(...)` и возвращает dict с
  `payload`;
- outbound media передается через `attachments=[...]`;
- `own_user_id()` читает `client.me.contact.id`;
- `users_cache_snapshot()` использует `client.users`;
- dialogs/groups/channels фильтруются по `Chat.type`;
- pymax imports остаются внутри backend boundary;
- MAX egress tests по HTTP CONNECT, redaction и fail-closed проходят.

Рекомендуемые targeted commands:

```bash
pytest tests/test_bridge_contracts.py
pytest tests/test_max_service_ports.py
pytest tests/test_max_adapter_leaves.py
pytest tests/test_max_egress.py
python -m compileall src/adapters/max/backends/pymax
pytest
```

Если full suite падает из-за внешнего окружения, зафиксировать причину в
handoff и отдельно указать, какие targeted tests прошли.

### Step 6: Local validation

Перед live MAX запуском:

1. Убедиться, что dependency installed в локальном venv.
2. Прогнать targeted tests.
3. Прогнать full `pytest`.
4. Проверить architecture boundary:

   ```bash
   rg -n "import pymax|from pymax" src tests docs
   ```

   Ожидаемо pymax imports должны быть только в backend package и тестах,
   которые явно проверяют backend behavior.

5. Проверить logs/redaction tests.
6. Проверить, что production config и secrets не изменены.

### Step 7: Live validation

Live validation выполнять только после session DB backup и успешных локальных
tests.

Проверить:

1. Auth или intentional session reuse.
2. `/status` показывает MAX connected.
3. Inbound MAX DM text приходит в правильный Telegram topic.
4. Inbound group text приходит в правильный Telegram topic.
5. Reply из Telegram отправляется обратно в MAX.
6. Inbound photo доставляется в Telegram.
7. Inbound file доставляется в Telegram.
8. Inbound video доставляется в Telegram с корректным CDN User-Agent.
9. Inbound voice/audio проходит через empty-message recovery.
10. Raw `CHAT_HISTORY` recovery работает для пропущенных media payloads.
11. Разрыв MAX connectivity приводит к внешнему reconnect и созданию свежего
    client.
12. При `home_ru_proxy` MAX использует Channel M egress.
13. При недоступном Channel M нет fallback на direct egress.
14. Logs не содержат message text, raw payload dumps, invite links, tokens,
    phone numbers или proxy credentials.

Результат live validation зафиксировать в PR/commit notes, но без приватного
контента.

## Точные adapter contracts

Эти contracts нужно сохранить, чтобы миграция не расползлась по проекту.

### Backend boundary

`MaxBackend` продолжает отдавать:

- raw PyMax client;
- `MaxClientPort` adapter;
- helpers для message/payload conversion;
- ping compatibility config или no-op behavior.

Все PyMax 2 imports остаются в:

```text
src/adapters/max/backends/pymax/
```

### Raw inbound contract

Bridge получает raw dict:

```python
{
    "opcode": <opcode>,
    "cmd": <cmd>,
    "seq": <seq>,
    "payload": <dict>,
}
```

`payload` не логировать целиком.

### Raw request contract

`PymaxClientAdapter.raw_request(...)` возвращает `None` или dict с ключом
`payload`.

Все private PyMax 2 calls, включая `client._app.invoke(...)`, остаются внутри
adapter.

### Message contract

Inbound message callbacks передают в operation services только
`MaxClientMessage`.

`message.sender` остается id пользователя, а не `User` object. Если PyMax 2
изменит форму поля, adapter обязан нормализовать ее до текущего DTO.

### Snapshot contracts

Snapshots возвращают DTO или plain values:

- `contacts_snapshot()` -> `list[MaxUserView]`;
- `users_cache_snapshot()` -> mapping/list, который текущие services уже
  ожидают;
- `dialogs_snapshot()` -> `list[MaxDialogView]`;
- `group_chats_snapshot()` -> `list[MaxChatView]`;
- `channels_snapshot()` -> `list[MaxChatView]`.

DM partner resolution не должен регрессировать для own-initiated DM.

### Send contract

`send_outbound_message(...)` сохраняет текущие аргументы и возвращает
`MaxSendResult`.

PyMax 2 attachment mechanics остаются internal detail:

```python
attachments=[Photo(...)]
attachments=[Video(...)]
attachments=[File(...)]
```

### Privacy contract

Запрещено логировать:

- message text;
- raw payload целиком;
- media bytes;
- signed CDN URLs;
- invite links;
- phone numbers;
- tokens;
- proxy credentials.

Errors должны проходить через существующие redaction helpers или не содержать
чувствительных значений.

## Риски и mitigation

| Риск | Уровень | Почему важно | Mitigation |
| --- | --- | --- | --- |
| Включился PyMax internal reconnect | Critical | В проекте reconnect должен создавать свежий client. Старое поведение pymax уже приводило к накоплению state. | Всегда `ExtraConfig(reconnect=False)`, unit test. |
| Включилась telemetry | High | Старый fake telemetry режим был проблемным, плюс privacy posture требует минимум лишнего трафика. | Всегда `ExtraConfig(telemetry=False)`, unit test. |
| Session DB несовместима | High | Первый live-run может изменить или сломать существующую session DB. | Backup перед live-run, по возможности новая session для первого теста. |
| Native `on_raw` приходит позже typed mapping | Medium | Старый patch ловил raw wrappers до потери части данных. | Unit test на conversion, live test voice/audio и raw history recovery. |
| `client.chats` иначе представляет dialogs/groups | Medium | Recovery и DM partner resolution зависят от реальных dialogs. | Фильтр по `Chat.type`, live test own-initiated DM. |
| `client._app.invoke` private | Medium | Raw history/audio/video зависят от raw requests. | Изолировать в adapter, покрыть узким test, не использовать вне backend. |
| MAX egress regression | High | MAX traffic должен идти через выбранный MAX-only egress и fail closed. | Сохранить `MaxEgressProfile.socket_connector`, egress/redaction tests. |
| Outbound voice/audio semantics | Medium | PyMax 2 public send API не выделяет voice-specific attachment. | На первом этапе `File` fallback, live test outbound media. |
| Pydantic aliases отличаются от ожиданий | Medium | Raw payload fields могут быть camelCase, DTO ожидает snake_case fields. | `Message.model_validate(...)`, DTO tests на ключевые поля. |
| Error classification drift | Low | `SocketNotConnectedError` исчез. | Классифицировать `ConnectionError`, `OSError`, `TimeoutError`, `PyMaxError`, `ApiError`. |

## Stop conditions

Остановить миграцию и не продолжать live rollout, если:

- PyMax 2 не может стартовать с `reconnect=False`;
- невозможно отключить telemetry;
- MAX session DB портится или требует непонятной миграции без backup recovery;
- raw `on_raw` не дает payload, достаточный для voice/audio recovery;
- Channel M egress нельзя сохранить без утечки credentials или fallback на
  direct;
- logs показывают message text, raw payload dumps или secrets;
- unit tests требуют изменений в `BridgeCore`, Telegram adapter или DB schema
  без явной новой причины;
- live validation ломает replies, topic routing или idempotency.

При stop condition вернуть dependency на `maxapi-python==1.2.5`, откатить
backend changes и восстановить session DB из backup при необходимости.

## Definition of Done

Миграция считается готовой, когда:

- работа выполнена в отдельной ветке, не в `master`;
- `requirements.txt` содержит `maxapi-python==2.0.1`;
- `ExtraConfig(reconnect=False, telemetry=False)` покрыт тестом;
- pymax imports остаются только внутри
  `src/adapters/max/backends/pymax/` и backend-specific tests;
- `BridgeCore`, Telegram adapter, DB schema и operation services не изменены
  без отдельно описанной причины;
- targeted tests проходят;
- full `pytest` проходит или есть понятное объяснение внешнего сбоя;
- live validation закрывает auth, inbound text, outbound replies, media,
  voice/audio recovery, raw history, reconnect, Channel M egress и privacy
  logs;
- rollback path проверен и понятен;
- production deploy и git push не выполнены без отдельной команды.

## Краткий итог для владельца проекта

Для перехода на PyMax 2 нужно не переписывать весь bridge, а заменить внутреннюю
реализацию MAX backend:

- обновить dependency;
- переписать `PymaxBackend` под `Client + ExtraConfig`;
- переписать `PymaxClientAdapter` под новые handler signatures, native
  `on_raw`, Pydantic v2 models и `attachments=[...]`;
- сохранить текущие ports, DTO, privacy rules и MAX egress;
- проверить unit tests и отдельно пройти live validation.

Кардинально менять архитектуру приложения не нужно. PyMax 2 дает более чистые
models, raw hooks, routing и transport stack, но для этого проекта безопаснее
использовать эти улучшения внутри backend boundary, а не перестраивать
`BridgeCore` и operation services вокруг PyMax abstractions.
