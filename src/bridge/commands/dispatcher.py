"""Bridge command registration boundary."""

from ..contracts import MaxBridgePort, TelegramBridgePort
from ...db.repository import Repository
from . import dm as bridge_dm_command


class BridgeCommandDispatcher:
    def __init__(
        self,
        *,
        tg: TelegramBridgePort,
        repo: Repository,
        max_adapter: MaxBridgePort,
        status_reporter,
        recovery_scheduler,
    ):
        self._tg = tg
        self._repo = repo
        self._max = max_adapter
        self._status = status_reporter
        self._recovery = recovery_scheduler

    def register(self):
        self._tg.on_command("status", self._status.build_status_message)
        self._tg.on_command("chats", self._status.build_chats_message)
        self._tg.on_command("help", self._status.build_help_message)
        self._tg.on_arg_command("dm", self.handle_dm, allow_group_general=True)
        self._tg.on_arg_command("recovery", self._recovery.handle_command)

    async def handle_dm(self, args: str) -> str:
        """Инициировать новый DM в MAX по имени пользователя."""
        return await bridge_dm_command.handle_dm(self._repo, self._max, args)
