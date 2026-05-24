from __future__ import annotations

from pymax import Client, ExtraConfig, SyncOverrides
from pymax.api.session.enums import DeviceType
from pymax.api.session.payloads import MobileUserAgentPayload
from pymax.auth import AuthFlow

from ...network import MaxEgressProfile
from .login import BridgeAuthService
from .session_store import BridgeSessionStore
from .transport import EgressClient, install_bridge_protocol_guards


def legacy_desktop_user_agent() -> MobileUserAgentPayload:
    return MobileUserAgentPayload(
        device_type=DeviceType.DESKTOP,
        app_version="25.12.14",
        os_version="Windows 10",
        timezone="Europe/Moscow",
        screen="1080x1920 1.0x",
        locale="ru",
        device_name="Chrome",
        device_locale="ru",
        build_number=0x97CB,
        header_user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )


def legacy_sync_overrides() -> SyncOverrides:
    return SyncOverrides(
        chats_sync=0,
        contacts_sync=0,
        drafts_sync=0,
        presence_sync=0,
    )


def make_extra_config(*, store=None) -> ExtraConfig:
    return ExtraConfig(
        reconnect=False,
        telemetry=False,
        store=store,
        user_agent=legacy_desktop_user_agent(),
        sync=legacy_sync_overrides(),
    )


def create_pymax_client(
    *,
    phone: str,
    data_dir: str,
    session_name: str,
    egress: MaxEgressProfile | None = None,
    extra_config: ExtraConfig | None = None,
    auth_flow: AuthFlow | None = None,
    import_legacy_session: bool = True,
):
    session_store = BridgeSessionStore(
        data_dir,
        session_name,
        phone=phone,
        import_legacy=import_legacy_session,
    )
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
    if auth_flow is not None:
        kwargs["auth_flow"] = auth_flow
    if egress is None:
        return _install_bridge_auth_service(Client(**kwargs))
    return _install_bridge_auth_service(
        EgressClient(**kwargs, socket_connector=egress.socket_connector)
    )


def _install_bridge_auth_service(client):
    app = getattr(client, "_app", None)
    api = getattr(app, "api", None)
    if api is not None:
        api.auth = BridgeAuthService(app)
    install_bridge_protocol_guards(client)
    return client
