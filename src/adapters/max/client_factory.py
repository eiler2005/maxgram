"""Compatibility MAX client factory."""


def create_socket_client(*, phone: str, data_dir: str, session_name: str):
    from .backends.pymax import PymaxBackend

    return PymaxBackend(
        phone=phone,
        data_dir=data_dir,
        session_name=session_name,
    ).create_client()
