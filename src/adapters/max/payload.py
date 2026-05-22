"""Pymax-free raw payload helpers."""

from typing import Optional


def payload_value(data: dict, *keys: str):
    normalized = {
        str(k).lower().replace("_", ""): v
        for k, v in data.items()
    }
    for key in keys:
        candidate = key.lower().replace("_", "")
        if candidate in normalized:
            return normalized[candidate]
    return None


def is_safe_field_name(name: object) -> bool:
    lowered = str(name).lower()
    blocked = ("url", "token", "text", "raw")
    return not any(marker in lowered for marker in blocked)


def safe_field_paths(value, *, max_depth: int = 2, max_items: int = 80) -> list[str]:
    paths: list[str] = []
    seen: set[int] = set()

    def iter_items(node):
        if isinstance(node, dict):
            return node.items()
        raw_fields = getattr(node, "__dict__", None)
        if isinstance(raw_fields, dict):
            return raw_fields.items()
        return ()

    def walk(node, prefix: str, depth: int):
        if node is None or depth > max_depth or len(paths) >= max_items:
            return
        if isinstance(node, (str, bytes, int, float, bool)):
            return
        if isinstance(node, (dict, list, tuple, set)) or hasattr(node, "__dict__"):
            node_id = id(node)
            if node_id in seen:
                return
            seen.add(node_id)

        if isinstance(node, (list, tuple, set)):
            if prefix:
                list_path = f"{prefix}[]"
                if list_path not in paths:
                    paths.append(list_path)
            for item in list(node)[:5]:
                walk(item, f"{prefix}[]" if prefix else "[]", depth + 1)
            return

        for key, child in iter_items(node):
            name = str(key)
            if name.startswith("_") or not is_safe_field_name(name):
                continue
            path = f"{prefix}.{name}" if prefix else name
            paths.append(path)
            if len(paths) >= max_items:
                return
            walk(child, path, depth + 1)

    walk(value, "", 0)
    return sorted(dict.fromkeys(paths))


def safe_payload_error_code(payload) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    error = payload_value(payload, "error", "errorCode", "code")
    if isinstance(error, dict):
        code = payload_value(error, "code", "name", "type", "error")
        return str(code)[:80] if code is not None else error.__class__.__name__
    if error is not None:
        return str(error)[:80]
    return None
