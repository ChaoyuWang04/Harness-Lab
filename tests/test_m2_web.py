from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]


def test_web_renders_terminal_answer_and_keeps_wrapped_raw_events() -> None:
    page = (LAB_ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="answer"' in page
    assert "JSON.parse(message.data)" in page
    assert "payload.answer" in page
    assert "回答失败" in page
    assert "white-space: pre-wrap" in page
    assert "overflow-wrap: anywhere" in page
    assert 'id="events"' in page
