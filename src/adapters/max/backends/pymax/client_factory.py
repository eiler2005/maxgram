from __future__ import annotations

from pymax import Client, ExtraConfig

from ...network import MaxEgressProfile
from .session_store import BridgeSessionStore
from .transport import EgressClient


def make_extra_config(*, store=None) -> ExtraConfig:
    return ExtraConfig(
        reconnect=False,
        telemetry=False,
        store=store,
    )


def create_pymax_client(
    *,
    phone: str,
    data_dir: str,
    session_name: str,
    egress: MaxEgressProfile | None = None,
    extra_config: ExtraConfig | None = None,
):
    session_store = BridgeSessionStore(data_dir, session_name, phone=phone)
    if extra_config is None:
        extra_config = make_extra_config(store=session_store)
    elif extra_config.store is None:
        extra_config = extra_config.model_copy(update={"store": session_store})
    kwargs = {
        "phone": phone,
        "work_dir": data_dir,
        "session_name": session_name,
        "extra_config": extra_config,
    }
    if egress is None:
        return Client(**kwargs)
    return EgressClient(**kwargs, socket_connector=egress.socket_connector)
