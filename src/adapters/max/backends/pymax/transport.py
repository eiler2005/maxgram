from __future__ import annotations

import asyncio
import socket
from typing import Any

import msgpack
from pymax.client import Client
from pymax.connection import ConnectionManager
from pymax.connection.readers import TCPReader
from pymax.protocol.tcp import TcpProtocol
from pymax.protocol.tcp.framing import TcpPacketFramer
from pymax.protocol.tcp.payload import MsgpackPayloadCodec
from pymax.transport.tcp import TCPTransport

from ...network import DirectSocketConnector
from ...network.egress import MaxSocketConnector
from .internals import pymax_client_connection, pymax_connection_protocol


class BridgeConnectionManager(ConnectionManager):
    """PyMax TCP connection guard for one-byte MAX sequence numbers."""

    def next_seq(self) -> int:
        seq = (int(getattr(self, "_seq", -1)) + 1) % 0x100
        setattr(self, "_seq", seq)
        return seq


class BridgeMsgpackPayloadCodec(MsgpackPayloadCodec):
    """PyMax msgpack codec tolerant to MAX maps with array-like keys."""

    def decode(self, payload_bytes: bytes) -> dict[Any, Any]:
        try:
            return super().decode(payload_bytes)
        except TypeError as exc:
            if "unhashable type" not in str(exc):
                raise
            return self._decode_with_hashable_keys(payload_bytes)

    def _decode_with_hashable_keys(self, payload_bytes: bytes) -> dict[Any, Any]:
        try:
            value = self._unpackb_with_pairs(payload_bytes, raw=False)
        except msgpack.exceptions.ExtraData as exc:
            if isinstance(exc.unpacked, dict):
                return exc.unpacked
            try:
                values = self._unpack_stream_with_pairs(payload_bytes, raw=False)
            except UnicodeDecodeError:
                values = self._unpack_stream_with_pairs(payload_bytes, raw=True)
            for item in values:
                if isinstance(item, dict):
                    return item
            raise
        except UnicodeDecodeError:
            value = self._unpackb_with_pairs(payload_bytes, raw=True)

        return value if isinstance(value, dict) else {}

    def _unpackb_with_pairs(self, payload_bytes: bytes, *, raw: bool) -> Any:
        return msgpack.unpackb(
            payload_bytes,
            raw=raw,
            strict_map_key=False,
            object_pairs_hook=self._pairs_to_dict,
        )

    def _unpack_stream_with_pairs(self, payload_bytes: bytes, *, raw: bool) -> list[Any]:
        unpacker = msgpack.Unpacker(
            raw=raw,
            strict_map_key=False,
            object_pairs_hook=self._pairs_to_dict,
        )
        unpacker.feed(payload_bytes)
        return list(unpacker)

    def _pairs_to_dict(self, pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
        return {self._hashable_key(key): value for key, value in pairs}

    def _hashable_key(self, key: Any) -> Any:
        if isinstance(key, list):
            return tuple(self._hashable_key(item) for item in key)
        if isinstance(key, dict):
            return tuple(
                sorted(
                    (
                        (
                            self._hashable_key(item_key),
                            self._hashable_key(item_value),
                        )
                        for item_key, item_value in key.items()
                    ),
                    key=repr,
                ),
            )
        try:
            hash(key)
        except TypeError:
            return repr(key)
        return key


def bridge_tcp_protocol() -> TcpProtocol:
    protocol = TcpProtocol()
    install_bridge_msgpack_codec(protocol)
    return protocol


def install_bridge_msgpack_codec(protocol) -> None:
    codec = BridgeMsgpackPayloadCodec()
    if hasattr(protocol, "serializer"):
        protocol.serializer = codec
    decoder = getattr(protocol, "payload_decoder", None)
    if decoder is not None:
        decoder.serializer = codec


def install_bridge_protocol_guards(client):
    connection = pymax_client_connection(client)
    install_bridge_sequence_guard(connection)
    protocol = pymax_connection_protocol(connection)
    if protocol is not None:
        install_bridge_msgpack_codec(protocol)
        client._maxtg_msgpack_guard_installed = True
    return client


def install_bridge_sequence_guard(connection) -> None:
    if connection is None:
        return
    if isinstance(connection, BridgeConnectionManager):
        connection._maxtg_seq_guard_installed = True
        return

    def next_seq() -> int:
        connection._seq = (getattr(connection, "_seq", -1) + 1) % 0x100
        return connection._seq

    connection.next_seq = next_seq
    connection._maxtg_seq_guard_installed = True


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
        return BridgeConnectionManager(
            reader=reader,
            transport=transport,
            protocol=bridge_tcp_protocol(),
        )
