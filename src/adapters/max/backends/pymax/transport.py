from __future__ import annotations

import asyncio
import socket

from pymax.client import Client
from pymax.connection import ConnectionManager
from pymax.connection.readers import TCPReader
from pymax.protocol.tcp import TcpProtocol
from pymax.protocol.tcp.framing import TcpPacketFramer
from pymax.transport.tcp import TCPTransport

from ...network import DirectSocketConnector
from ...network.egress import MaxSocketConnector


class EgressTCPTransport(TCPTransport):
    """PyMax 2 TCP transport that opens sockets through bridge MAX egress."""

    def __init__(
        self,
        *,
        socket_connector: MaxSocketConnector | None,
        host: str,
        port: int,
        use_ssl: bool = True,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(host=host, port=port, proxy=None, use_ssl=use_ssl)
        self._maxtg_socket_connector = socket_connector or DirectSocketConnector()
        self._maxtg_timeout = timeout

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        raw_sock = await loop.run_in_executor(
            None,
            lambda: self._maxtg_socket_connector.connect(
                self._host,
                self._port,
                timeout=self._maxtg_timeout,
            ),
        )
        raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        raw_sock.setblocking(False)
        self._reader, self._writer = await asyncio.open_connection(
            sock=raw_sock,
            ssl=self._use_ssl,
            server_hostname=self._host if self._use_ssl else None,
        )


class EgressClient(Client):
    """PyMax 2 Client variant that preserves configured MAX-only egress."""

    def __init__(self, *args, socket_connector: MaxSocketConnector | None = None, **kwargs):
        self._maxtg_socket_connector = socket_connector or DirectSocketConnector()
        super().__init__(*args, **kwargs)

    def _build_connection(self) -> ConnectionManager:
        transport = EgressTCPTransport(
            socket_connector=self._maxtg_socket_connector,
            host=self.extra_config.host,
            port=self.extra_config.port,
            use_ssl=self.extra_config.use_ssl,
        )
        reader = TCPReader(
            transport=transport,
            framer=TcpPacketFramer(),
        )
        return ConnectionManager(
            reader=reader,
            transport=transport,
            protocol=TcpProtocol(),
        )
