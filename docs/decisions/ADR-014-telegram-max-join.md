# ADR-014: Подтверждённое вступление в MAX из Telegram

Статус: принято. Дата: 2026-11-03.

## Контекст

Владелец получает MAX-ссылки в Telegram и хочет подключать каналы/группы
существующим MAX-аккаунтом bridge, сразу получая Telegram-топик. Join меняет
членство аккаунта и может завершиться неоднозначно при потере соединения.
Первое сообщение нового чата может прийти одновременно с созданием топика.

## Решение

- Принимать `/join ссылка` только от владельца в DM/General; вставленные или
  пересланные ссылки распознавать только в owner DM. Обычная пересылка URL из
  форум-топика не запускает вступление.
- Показывать confirm/cancel на 15 минут. Использовать существующую private
  таблицу callback actions с новым `max_join_request`, случайным callback id
  и минимальным payload `url`. Доступ повторно проверять на callback.
- Атомарный SQL claim переводит `pending → processing`. Завершение даёт
  `used|failed` и очищает payload. При timeout/crash не делать автоматический
  replay: MAX мог применить запрос до потери ответа. Просроченные payload
  очищать при следующем prepare/нажатии. Это не exactly-once гарантия MAX API.
- `MaxJoinService` владеет этим flow без aiogram/PyMax imports. Транспортный
  результат — `TelegramResponse`; membership — `MaxJoinedChat.join_status`,
  безопасная ошибка — `MaxJoinError.code`. Backend выбирает один PyMax endpoint
  по типу ссылки и не повторяет неопределённый запрос на другом endpoint.
- Создавать/переиспользовать обычный binding только после подтверждённого
  membership и пригодного chat id. Заявка на одобрение отличается от успеха.
  Общий per-chat lock в core сериализует join и первый inbound event.
  Routing metadata не создаёт фиктивного message_map row.
- Ограниченный success cache (60с, 256 записей) позволяет завершить topic
  creation и сгладить повторный запрос. После истечения membership проверяется
  снова. Existing binding сохраняет свой режим. История не импортируется.

## Последствия и проверка

Схема DB и конфиг не меняются, второй MAX-клиент не нужен. Pending actions
переживают рестарт, processing actions требуют проверки владельцем.
Приватная ссылка временно находится в SQLite и ограниченном in-memory cache;
в callback/logs/status/health/exports она не передаётся.
Legacy MAX-message invite actions остаются совместимыми и используют прежний
recovery flow. Набор unit/SQLite и fake-backend сквозных тестов проверяет
права, parsing, TTL, гонки, privacy, timeout и совместное создание топика.

Инструкция: [runbook](../runbooks/max-join.md).
