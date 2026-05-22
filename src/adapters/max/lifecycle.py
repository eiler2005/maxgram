from __future__ import annotations

import asyncio
import logging
import time

from .client_factory import create_socket_client
from ...logging_utils import log_event, mask_phone, sanitize_path

logger = logging.getLogger("src.adapters.max_adapter")


class MaxLifecycleMixin:
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

            while getattr(client, "is_connected", False):
                try:
                    await client._send_and_wait(
                        opcode=ping_opcode,
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
        try:
            from pymax.exceptions import SocketNotConnectedError
            from pymax.static.constant import DEFAULT_PING_INTERVAL
            from pymax.static.enum import Opcode
        except Exception as e:
            logger.warning("Could not install fail-fast interactive ping loop: %s", e)
            return client

        client._send_interactive_ping = self._build_failfast_interactive_ping(
            client,
            ping_interval=DEFAULT_PING_INTERVAL,
            failure_limit=self._interactive_ping_failure_limit,
            ping_opcode=Opcode.PING,
            disconnect_error=SocketNotConnectedError,
        )
        logger.debug(
            "Installed fail-fast interactive ping loop failure_limit=%s interval=%ss",
            self._interactive_ping_failure_limit,
            DEFAULT_PING_INTERVAL,
        )
        return client

    async def _make_client(self):
        """Создать свежий SocketMaxClient (без накопленного кеша)."""
        client = create_socket_client(
            phone=self._phone,
            data_dir=self._data_dir,
            session_name=self._session_name,
        )
        self._wrap_client_stage(client, "_sync")
        self._wrap_client_stage(client, "_login")
        client = self._install_raw_message_interceptor(client)
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
                self._recover_session_if_needed(first_connect=first_connect)
                self._client = await self._make_client()

                async def _on_start():
                    nonlocal first_connect
                    self._started = True
                    self._last_connected_at = int(time.time())
                    self._clear_runtime_issue()
                    self._backup_session_snapshot(first_connect=first_connect)
                    log_event(
                        logger,
                        logging.INFO,
                        "max.adapter.connected",
                        stage="startup" if first_connect else "runtime",
                        outcome="connected",
                    )
                    # Получаем ID собственного аккаунта для фильтрации эхо
                    try:
                        me = self._client.me
                        if me:
                            self._own_id = str(getattr(me, "id", None) or "")
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

                    self._start_pending_empty_recovery_worker()

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

                self._client.on_start(_on_start)
                if hasattr(self._client, "on_raw_receive"):
                    self._client.on_raw_receive(self._handle_raw_receive)
                    log_event(
                        logger,
                        logging.INFO,
                        "max.raw.handler_registered",
                        stage="startup" if first_connect else "runtime",
                        outcome="registered",
                        raw_handler_count=len(getattr(self._client, "_on_raw_receive_handlers", []) or []),
                    )
                self._client.on_message()(self._handle_raw_message)
                self._client.on_message_edit()(self._handle_raw_message)
                self._client.on_message_delete()(self._handle_raw_message)

                log_event(
                    logger,
                    logging.INFO,
                    "max.adapter.starting",
                    stage="startup" if first_connect else "runtime",
                    outcome="started",
                    phone=mask_phone(self._phone),
                )
                await self._client.start()

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
                    await self._capture_runtime_error(e)
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
        return self._started
