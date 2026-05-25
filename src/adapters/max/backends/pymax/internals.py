"""Centralized access to PyMax private runtime shape."""

from __future__ import annotations


class PymaxInternalsContractError(RuntimeError):
    """Raised when an expected PyMax private runtime shape is unavailable."""


def pymax_client_app(client: object) -> object | None:
    return getattr(client, "_app", None)


def pymax_client_connection(client: object) -> object | None:
    connection = getattr(client, "_connection", None)
    if connection is not None:
        return connection

    connection = getattr(client, "_conn", None)
    if connection is not None:
        return connection

    app = pymax_client_app(client)
    connection = getattr(app, "connection", None)
    if connection is not None:
        return connection
    return getattr(app, "conn", None)


def pymax_connection_lost(connection: object) -> bool:
    return bool(getattr(connection, "_conn_lost", False))


def pymax_connection_is_open(connection: object) -> bool:
    is_open = getattr(connection, "is_open", False)
    if callable(is_open):
        is_open = is_open()
    return bool(is_open)


def pymax_connection_transport_connected(connection: object) -> bool:
    transport = getattr(connection, "transport", None)
    if transport is None:
        return True
    connected = getattr(transport, "connected", None)
    if callable(connected):
        connected = connected()
    return True if connected is None else bool(connected)


def pymax_connection_protocol(connection: object) -> object | None:
    return getattr(connection, "protocol", None)
