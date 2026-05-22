"""`/dm` command handler."""

from typing import Optional

from ...db.repository import Repository
from ..contracts import MaxBridgePort


async def handle_dm(repo: Repository, max_adapter: MaxBridgePort, args: str) -> str:
    words = args.strip().split()
    if len(words) < 2:
        return (
            "⚠️ Формат: /dm Имя Фамилия текст сообщения\n"
            "Пример: /dm Татьяна Геннадиевна Ладина Добрый день!"
        )

    found_user_id: Optional[str] = None
    found_name: Optional[str] = None
    message_text: Optional[str] = None

    for name_len in range(min(4, len(words) - 1), 0, -1):
        candidate_name = " ".join(words[:name_len])
        candidate_msg = " ".join(words[name_len:])
        if not candidate_msg.strip():
            continue
        uid = await repo.find_user_by_name(candidate_name)
        if not uid:
            uid = max_adapter.find_user_by_name(candidate_name)
        if uid:
            found_user_id = uid
            found_name = candidate_name
            message_text = candidate_msg
            break

    if not found_user_id:
        preview = " ".join(words[:3])
        return (
            f"❌ Пользователь не найден: «{preview}…»\n"
            "Имя должно совпадать с отображаемым в MAX.\n"
            "Пользователь должен быть в контактах или ранее писать в известные чаты."
        )

    sent_id = await max_adapter.send_message(
        chat_id=found_user_id,
        text=message_text,
        flow_id="tg_cmd_dm",
    )
    if sent_id:
        return f"✅ Сообщение отправлено {found_name}. Топик появится автоматически."
    return f"❌ Не удалось отправить сообщение {found_name}."
