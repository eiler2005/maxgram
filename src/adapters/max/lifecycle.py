from __future__ import annotations

import asyncio
import logging
import time

from .deps import LifecycleDeps
from ...logging_utils import log_event, mask_phone, sanitize_path

logger = logging.getLogger("src.adapters.max_adapter")


class MaxLifecycleService:
    def __init__(self, deps: LifecycleDeps):
        self._deps = deps

    @property
    def _backend(self):
        return self._deps.backend

    @property
    def _phone(self):
        return self._deps.phone

    @property
    def _start_handlers(self):
        return self._deps.start_handlers

    @property
    def _interactive_ping_failure_limit(self):
        return self._deps.interactive_ping_failure_limit

    @property
    def _client(self):
        return self._deps.connection.client

    @_client.setter
    def _client(self, value):
        self._deps.connection.client = value

    @property
    def _started(self):
        return self._deps.connection.started

    @_started.setter
    def _started(self, value):
        self._deps.connection.started = value

    @property
    def _own_id(self):
        return self._deps.connection.own_id

    @_own_id.setter
    def _own_id(self, value):
        self._deps.connection.own_id = value

    @property
    def _last_start_error(self):
        return self._deps.connection.last_start_error

    @property
    def _last_issue(self):
        return self._deps.connection.last_issue

    @property
    def _last_connected_at(self):
        return self._deps.connection.last_connected_at

    @_last_connected_at.setter
    def _last_connected_at(self, value):
        self._deps.connection.last_connected_at = value

    def _build_failfast_interactive_ping(self, client, *, ping_interval: float,
                                         failure_limit: int, ping_opcode,
                                         disconnect_error):
        """Создать ping loop, который форсирует reconnect после серии ошибок.

        Upstream pymax логирует `Interactive ping failed`, но сам reconnect не
        инициирует. В результате сокет может висеть в полуживом состоянии
        несколько минут и терять входящие события. Здесь после N подряд ошибок
        мы закрываем клиента и отдаём управление нашему outer reconnect loop.
        """
        normalized_interval = max(0.0, float(ping_interval))
        normalized_limit = max(1, int(failure_limit))

        async def _send_interactive_ping() -> None:
            consecutive_failures = 0

            while client.is_connected:
                try:
                    ping_opcode_value = getattr(ping_opcode, "value", ping_opcode)
                    try:
                        default_ping_opcode = int(ping_opcode_value)
                    except (TypeError, ValueError):
                        default_ping_opcode = None
                    await client.raw_request(
                        opcode_name="PING",
                        default_opcode=default_ping_opcode,
                        payload={"interactive": True},
                        cmd=0,
                    )
                    if consecutive_failures:
                        client.logger.info(
                            "Interactive ping recovered after %s failure(s)",
                            consecutive_failures,
                        )
                    consecutive_failures = 0
                    client.logger.debug("Interactive ping sent successfully")
                except disconnect_error:
                    client.logger.debug("Socket disconnected, exiting ping loop")
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_failures += 1
                    client.logger.warning(
                        "Interactive ping failed (%s/%s): %s",
                        consecutive_failures,
                        normalized_limit,
                        exc,
                    )
                    if consecutive_failures >= normalized_limit:
                        client.logger.error(
                            "Interactive ping failure limit reached (%s), forcing reconnect",
                            normalized_limit,
                        )
                        try:
                            await client.close()
                        except Exception:
                            client.logger.exception(
                                "Failed to close MAX client after ping failure limit"
                            )
                        break

                await asyncio.sleep(normalized_interval)

        return _send_interactive_ping

    def _install_failfast_interactive_ping(self, client):
        get_config = getattr(self._backend, "failfast_ping_config", None)
        try:
            config = get_config() if callable(get_config) else None
        except Exception as e:
            logger.warning("Could not install fail-fast interactive ping loop: %s", e)
            return client
        if not config:
            return client

        client.install_interactive_ping(self._build_failfast_interactive_ping(
            client,
            ping_interval=float(config["ping_interval"]),
            failure_limit=self._interactive_ping_failure_limit,
            ping_opcode=config["ping_opcode"],
            disconnect_error=config["disconnect_error"],
        ))
        logger.debug(
            "Installed fail-fast interactive ping loop failure_limit=%s interval=%ss",
            self._interactive_ping_failure_limit,
            config["ping_interval"],
        )
        return client

    async def _make_client(self):
        """Создать свежий MAX client port (без накопленного кеша)."""
        client = self._backend.create_client()
        client.prepare_startup(self._deps.runtime._capture_runtime_error)
        client = self._deps.events._install_raw_message_interceptor(client)
        return self._install_failfast_interactive_ping(client)

    async def start(self):
        """Запустить клиент с собственным reconnect-циклом.

        reconnect=False в pymax + outer loop: каждый раз создаём свежий клиент,
        чтобы не накапливать кеш dialogs/chats (pymax bug при reconnect=True).
        """
        retry_delay = 5
        first_connect = True

        while True:
            failure_logged = False
            try:
                self._deps.recovery._recover_session_if_needed(first_connect=first_connect)
                self._client = await self._make_client()

                async def _on_start():
                    nonlocal first_connect
                    self._started = True
                    self._last_connected_at = int(time.time())
                    self._deps.runtime._clear_runtime_issue()
                    self._deps.recovery._backup_session_snapshot(first_connect=first_connect)
                    log_event(
                        logger,
                        logging.INFO,
                        "max.adapter.connected",
                        stage="startup" if first_connect else "runtime",
                        outcome="connected",
                    )
                    # Получаем ID собственного аккаунта для фильтрации эхо
                    try:
                        own_id = self._client.own_user_id() if self._client else None
                        if own_id:
                            self._own_id = own_id
                        else:
                            log_event(
                                logger,
                                logging.WARNING,
                                "max.adapter.own_id_missing",
                                stage="startup",
                                outcome="warning",
                            )
                    except Exception as e:
                        log_event(
                            logger,
                            logging.WARNING,
                            "max.adapter.own_id_failed",
                            stage="startup",
                            outcome="warning",
                            error=str(e),
                        )

                    self._deps.voice_recovery._start_pending_empty_recovery_worker()

                    handler_stage = "startup" if first_connect else "runtime"
                    if not first_connect:
                        log_event(
                            logger,
                            logging.INFO,
                            "max.adapter.reconnected",
                            stage="runtime",
                            outcome="connected",
                        )
                    first_connect = False
                    for h in self._start_handlers:
                        try:
                            await h()
                        except Exception as e:
                            log_event(
                                logger,
                                logging.ERROR,
                                "max.adapter.start_handler_failed",
                                stage=handler_stage,
                                outcome="failed",
                                error=str(e),
                            )

                self._client.register_start_handler(_on_start)
                raw_handler_count = self._client.register_raw_receive_handler(
                    self._deps.events._handle_raw_receive
                )
                if raw_handler_count is not None:
                    log_event(
                        logger,
                        logging.INFO,
                        "max.raw.handler_registered",
                        stage="startup" if first_connect else "runtime",
                        outcome="registered",
                        raw_handler_count=raw_handler_count,
                    )
                self._client.register_message_handler(self._deps.events._handle_raw_message)
                self._client.register_message_edit_handler(self._deps.events._handle_raw_message)
                self._client.register_message_delete_handler(self._deps.events._handle_raw_message)
                self._client.register_typing_handler(self._deps.events._handle_typing)
                self._client.register_message_read_handler(self._deps.events._handle_message_read)
                self._client.register_presence_handler(self._deps.events._handle_presence)
                self._client.register_reaction_update_handler(self._deps.events._handle_reaction_update)

                log_event(
                    logger,
                    logging.INFO,
                    "max.adapter.starting",
                    stage="startup" if first_connect else "runtime",
                    outcome="started",
                    phone=mask_phone(self._phone),
                )
                await self._client.start()

                if not self._started and not self._last_start_error:
                    await self._deps.runtime._capture_runtime_error(
                        RuntimeError("MAX client start returned before on_start")
                    )

                if not self._started and self._last_start_error:
                    issue = self._last_issue
                    log_event(
                        logger,
                        logging.ERROR,
                        "max.adapter.failed",
                        stage="runtime",
                        outcome="failed",
                        reason="client_error",
                        error=self._last_start_error,
                        issue_kind=issue.kind if issue is not None else None,
                        requires_reauth=issue.requires_reauth if issue is not None else False,
                    )
                    failure_logged = True
            except Exception as e:
                if self._last_start_error != (str(e).strip() or e.__class__.__name__):
                    await self._deps.runtime._capture_runtime_error(e)
                issue = self._last_issue
                log_event(
                    logger,
                    logging.ERROR,
                    "max.adapter.failed",
                    stage="runtime",
                    outcome="failed",
                    reason="client_error",
                    error=self._last_start_error,
                    issue_kind=issue.kind if issue is not None else None,
                    requires_reauth=issue.requires_reauth if issue is not None else False,
                )
                failure_logged = True

            # Клиент завершился — ждём перед перезапуском
            if not failure_logged and not self._started and self._last_start_error:
                issue = self._last_issue
                log_event(
                    logger,
                    logging.ERROR,
                    "max.adapter.failed",
                    stage="runtime",
                    outcome="failed",
                    reason="client_error",
                    error=self._last_start_error,
                    issue_kind=issue.kind if issue is not None else None,
                    requires_reauth=issue.requires_reauth if issue is not None else False,
                )
            log_event(
                logger,
                logging.INFO,
                "max.adapter.reconnecting",
                stage="runtime",
                outcome="retrying",
                retry_in_seconds=retry_delay,
            )
            self._started = False
            await asyncio.sleep(retry_delay)

    def is_ready(self) -> bool:
        if not self._started:
            return False
        client = self._client
        if client is None:
            return False

        is_connected = getattr(client, "is_connected", None)
        if is_connected is None:
            return False
        if callable(is_connected):
            is_connected = is_connected()
        return bool(is_connected)

    async def close(self):
        self._started = False
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            logger.exception("Failed to close MAX client during shutdown")
