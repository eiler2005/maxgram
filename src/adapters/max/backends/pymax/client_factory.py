from __future__ import annotations

from pymax import Client, ExtraConfig

from ...network import MaxEgressProfile
from .transport import EgressClient


def make_extra_config() -> ExtraConfig:
    return ExtraConfig(
        reconnect=False,
        telemetry=False,
    )


def create_pymax_client(
    *,
    phone: str,
    data_dir: str,
    session_name: str,
    egress: MaxEgressProfile | None = None,
):
    extra_config = make_extra_config()
    kwargs = {
        "phone": phone,
        "work_dir": data_dir,
        "session_name": session_name,
        "extra_config": extra_config,
    }
    if egress is None:
        return Client(**kwargs)
    return EgressClient(**kwargs, socket_connector=egress.socket_connector)
