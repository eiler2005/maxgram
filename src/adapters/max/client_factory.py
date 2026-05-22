"""Pymax-bound MAX client factory."""


def create_socket_client(*, phone: str, data_dir: str, session_name: str):
    from pymax import SocketMaxClient

    return SocketMaxClient(
        phone=phone,
        work_dir=data_dir,
        session_name=session_name,
        reconnect=False,
        send_fake_telemetry=False,
    )
