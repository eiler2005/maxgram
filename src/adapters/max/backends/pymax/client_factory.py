from __future__ import annotations

from pymax import ExtraConfig, SyncOverrides
from pymax.api.session.enums import DeviceType
from pymax.api.session.payloads import MobileUserAgentPayload
from pymax.auth import AuthFlow
from pymax.fingerprint.models import ApkBuildFingerprint
from pymax.versions.catalog import VersionCatalog

from ...network import MaxEgressProfile
from .session_store import BridgeSessionStore
from .transport import BridgeClient, EgressClient

PINNED_MAX_APP_VERSION = "26.35.1"
PINNED_MAX_APP_FINGERPRINT = ApkBuildFingerprint(
    signature_scheme="v3",
    certificate_count=1,
    certificate_meta_sha256="1684414033eb263e2c615f8b7df5ed8793850a07656304997fbf07e9e21e1e93",
    certificate_sha256=["1684414033eb263e2c615f8b7df5ed8793850a07656304997fbf07e9e21e1e93"],
    dex_meta_sha256="58593332e67b8f80fe708f139f43ad65d654ebeea768adf2ea2b1611a54f19b5",
    so_meta_sha256_arm64_v8a="fdaf7841b16e6ddc64092d188a6202a37a525c0d77d6b773d63f9be6e0ac2529",
    so_meta_sha256={
        "arm64-v8a": "fdaf7841b16e6ddc64092d188a6202a37a525c0d77d6b773d63f9be6e0ac2529",
        "armeabi-v7a": "dfa9e6141ec1b96f2addf6900538b992db47b1acc33f1a22adc9378b98f104e8",
        "x86": "b4892273ee29c8882e81182bd961c240c5376426ae81919495c16b6ed620ec14",
        "x86_64": "c1e2afc2171243a694c25db6694f664540a3e146dcdeb2b1baba090ebcdee289",
    },
    build_number=6867,
)


def bridge_version_catalog() -> VersionCatalog:
    """Return bundled fingerprints plus the current MAX-required release."""
    catalog = VersionCatalog()
    if PINNED_MAX_APP_VERSION not in catalog.versions:
        catalog.add(PINNED_MAX_APP_VERSION, PINNED_MAX_APP_FINGERPRINT)
    return catalog


def latest_catalog_app_version(catalog: VersionCatalog | None = None) -> str:
    """Return the newest numeric app version bundled with PyMax.

    ``VersionCatalog.recommended()`` can lag behind profiles that are already
    bundled with the installed library.  MAX rejects that stale value before it
    sends an SMS challenge, so the legacy desktop profile must use the newest
    compatible catalog entry instead.
    """
    catalog = catalog or bridge_version_catalog()
    return max(
        catalog.versions,
        key=lambda version: tuple(int(component) for component in version.split(".")),
    )


def legacy_desktop_user_agent(
    catalog: VersionCatalog | None = None,
) -> MobileUserAgentPayload:
    """Keep the legacy desktop shape on a version MAX still accepts."""
    catalog = catalog or bridge_version_catalog()
    app_version = latest_catalog_app_version(catalog)
    build_number = catalog.resolve(app_version).build_number
    return MobileUserAgentPayload(
        device_type=DeviceType.DESKTOP,
        app_version=app_version,
        os_version="Windows 10",
        timezone="Europe/Moscow",
        screen="1080x1920 1.0x",
        locale="ru",
        device_name="Chrome",
        device_locale="ru",
        build_number=build_number,
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


def make_extra_config(*, store=None, catalog: VersionCatalog | None = None) -> ExtraConfig:
    return ExtraConfig(
        reconnect=False,
        relogin=False,
        telemetry=False,
        store=store,
        user_agent=legacy_desktop_user_agent(catalog),
        sync=legacy_sync_overrides(),
    )


def make_sms_reauth_extra_config() -> ExtraConfig:
    """Use a current Android device profile for an explicit SMS reauth.

    The legacy desktop shape remains the regular runtime profile so valid
    migrated sessions are not changed. MAX can reject a fresh AUTH_REQUEST
    from that legacy shape even with a current Android fingerprint.
    """
    return ExtraConfig(
        reconnect=False,
        relogin=False,
        telemetry=False,
        device_type=DeviceType.ANDROID,
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
    version_catalog = bridge_version_catalog()
    session_store = BridgeSessionStore(
        data_dir,
        session_name,
        phone=phone,
        import_legacy=import_legacy_session,
    )
    if extra_config is None:
        extra_config = make_extra_config(store=session_store, catalog=version_catalog)
    elif extra_config.store is None:
        extra_config = extra_config.model_copy(update={"store": session_store})
    if egress is not None:
        from yarl import URL
        options = egress.http_client_options
        proxy = options.proxy
        if proxy and options.proxy_auth:
            proxy = str(URL(proxy).with_user(options.proxy_auth.login).with_password(options.proxy_auth.password))
        # TCP uses our connector; PyMax uploads use app.config.proxy.
        extra_config = extra_config.model_copy(update={"proxy": proxy})
    app_version = (
        extra_config.user_agent.app_version
        if extra_config.user_agent is not None
        else latest_catalog_app_version(version_catalog)
    )
    kwargs = {
        "phone": phone,
        "work_dir": data_dir,
        "session_name": session_name,
        "extra_config": extra_config,
        # ClientConfig.version and the handshake user-agent must describe the
        # same profile.  Passing only ExtraConfig.user_agent leaves PyMax on
        # its stale default version internally and MAX rejects AUTH_REQUEST.
        "app_version": app_version,
        "catalog": version_catalog,
    }
    if auth_flow is not None:
        kwargs["auth_flow"] = auth_flow
    if egress is None:
        return BridgeClient(**kwargs)
    return EgressClient(**kwargs, socket_connector=egress.socket_connector)
