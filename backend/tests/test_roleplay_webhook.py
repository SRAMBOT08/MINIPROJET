"""Unit tests for Role Play Agora webhook payload parsing."""
from app.role_play.webhook import parse_roleplay_voice_turns


def test_parse_roleplay_voice_turns_extracts_final_candidate_turns():
    payload = {"payload": {"channel": "roleplay-x", "turns": [
        {"role": "user", "content": "I understand your frustration.", "turn_id": "t1", "is_final": True},
        {"role": "assistant", "content": "That's not good enough.", "turn_id": "t2"},
        {"role": "user", "content": "  ", "turn_id": "t3"},
    ]}}
    turns = parse_roleplay_voice_turns(payload)
    assert turns == [{"turn_id": "t1", "text": "I understand your frustration."}]


def test_parse_roleplay_voice_turns_skips_non_final_updates():
    payload = {"payload": {"turns": [
        {"role": "user", "content": "partial fragm", "turn_id": "t1", "is_final": False},
    ]}}
    assert parse_roleplay_voice_turns(payload) == []


def test_parse_roleplay_voice_turns_handles_contents_shape():
    payload = {"payload": {"contents": [
        {"role": "user", "content": "Let's find a solution together.", "turn_id": "t5"},
    ]}}
    assert parse_roleplay_voice_turns(payload) == [{"turn_id": "t5", "text": "Let's find a solution together."}]
