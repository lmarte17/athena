import json

import pytest

from app.ws_upstream import dispatch_upstream_frame


class _FakeQueue:
    def __init__(self) -> None:
        self.realtime = []
        self.contents = []
        self.activity_start_calls = 0
        self.activity_end_calls = 0

    def send_realtime(self, blob):
        self.realtime.append(blob)

    def send_content(self, content):
        self.contents.append(content)

    def send_activity_start(self):
        self.activity_start_calls += 1

    def send_activity_end(self):
        self.activity_end_calls += 1


def test_dispatch_upstream_frame_audio_bytes():
    queue = _FakeQueue()
    dispatch_upstream_frame({"bytes": b"\x00\x01"}, queue)

    assert len(queue.realtime) == 1
    assert queue.realtime[0].data == b"\x00\x01"
    assert queue.realtime[0].mime_type == "audio/pcm;rate=16000"


def test_dispatch_upstream_frame_text_content():
    queue = _FakeQueue()
    frame = {"text": json.dumps({"type": "text", "text": "hello"})}
    dispatch_upstream_frame(frame, queue)

    assert len(queue.contents) == 1
    content = queue.contents[0]
    assert content.role == "user"
    assert content.parts[0].text == "hello"


def test_dispatch_upstream_frame_image_content():
    queue = _FakeQueue()
    frame = {
        "text": json.dumps({
            "type": "image",
            "data": "aGVsbG8=",  # "hello"
            "mime_type": "image/png",
        })
    }
    dispatch_upstream_frame(frame, queue)

    assert len(queue.realtime) == 1
    assert queue.realtime[0].data == b"hello"
    assert queue.realtime[0].mime_type == "image/png"


def test_dispatch_upstream_frame_activity_signals():
    queue = _FakeQueue()
    dispatch_upstream_frame({"text": json.dumps({"type": "activity_start"})}, queue)
    dispatch_upstream_frame({"text": json.dumps({"type": "activity_end"})}, queue)

    assert queue.activity_start_calls == 0
    assert queue.activity_end_calls == 0


def test_dispatch_upstream_frame_ignores_unknown_kind():
    queue = _FakeQueue()
    dispatch_upstream_frame({"text": json.dumps({"type": "noop"})}, queue)

    assert queue.realtime == []
    assert queue.contents == []
    assert queue.activity_start_calls == 0
    assert queue.activity_end_calls == 0


def test_dispatch_upstream_frame_invalid_json_raises():
    queue = _FakeQueue()
    with pytest.raises(json.JSONDecodeError):
        dispatch_upstream_frame({"text": "not-json"}, queue)
