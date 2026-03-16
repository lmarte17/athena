import json

import pytest

from app.ws_logic import TurnAccumulator, has_turn_content, parse_client_text_message


def test_turn_accumulator_merges_chunks_and_resets():
    acc = TurnAccumulator()

    acc.add_input("hello")
    acc.add_input("hello there")
    acc.add_output("hi")
    acc.add_output("alex")

    transcript_in, transcript_out = acc.finalize()
    assert transcript_in == "hello there"
    assert transcript_out == "hi alex"

    # finalize() resets buffers
    empty_in, empty_out = acc.finalize()
    assert empty_in == ""
    assert empty_out == ""


def test_turn_accumulator_interrupt_discards_partial_turn():
    acc = TurnAccumulator()
    acc.add_input("partial user")
    acc.add_output("partial athena")

    acc.interrupt()
    transcript_in, transcript_out = acc.finalize()
    assert transcript_in == ""
    assert transcript_out == ""


def test_parse_client_text_message_returns_kind_and_message():
    raw = json.dumps({"type": "text", "text": "hello"})
    kind, msg = parse_client_text_message(raw)
    assert kind == "text"
    assert msg["text"] == "hello"


def test_parse_client_text_message_handles_missing_type():
    kind, msg = parse_client_text_message(json.dumps({"foo": "bar"}))
    assert kind is None
    assert msg["foo"] == "bar"


def test_parse_client_text_message_invalid_json_raises():
    with pytest.raises(json.JSONDecodeError):
        parse_client_text_message("not json")


def test_has_turn_content():
    assert has_turn_content("u", "")
    assert has_turn_content("", "a")
    assert has_turn_content("u", "a")
    assert not has_turn_content("", "")
