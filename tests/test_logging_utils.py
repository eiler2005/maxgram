import io
import json
import logging

from src.logging_utils import EventFormatter, log_event, sanitize_preview, sanitize_url


def _render_event(fmt_mode: str) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(EventFormatter(fmt_mode=fmt_mode))

    logger = logging.getLogger(f"tests.logging_utils.{fmt_mode}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        log_event(
            logger,
            logging.INFO,
            "bridge.test",
            flow_id="mx:-1:42",
            direction="inbound",
            attachments=[{"kind": "photo", "filename": "clip.jpg"}],
        )
    finally:
        logger.removeHandler(handler)

    return stream.getvalue().strip()


def test_sanitize_preview_masks_digits_and_newlines():
    preview = sanitize_preview("Привет 1234567890\nмир\x01", limit=30)

    assert preview == "Привет 12***90\\nмир"


def test_sanitize_url_strips_query_parameters():
    assert sanitize_url("https://cdn.example.com/video.mp4?token=secret&id=1") == "cdn.example.com/video.mp4"


def test_event_formatter_mixed_renders_key_value_fields():
    rendered = _render_event("mixed")

    assert "event=bridge.test" in rendered
    assert "flow_id=mx:-1:42" in rendered
    assert 'attachments=[{"filename":"clip.jpg","kind":"photo"}]' in rendered


def test_event_formatter_json_renders_valid_json_line():
    rendered = _render_event("json")
    payload = json.loads(rendered)

    assert payload["event"] == "bridge.test"
    assert payload["flow_id"] == "mx:-1:42"
    assert payload["attachments"] == [{"filename": "clip.jpg", "kind": "photo"}]
