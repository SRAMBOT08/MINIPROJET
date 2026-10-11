"""Unit tests for GD Agora webhook signature verification and payload parsing."""
import hashlib
import hmac
import json
from unittest.mock import patch

from app.core.config import settings
from app.group_discussion.webhook import parse_gd_voice_events, verify_agora_signature


def test_signature_passes_when_secret_is_unconfigured():
    with patch.object(settings, "AGORA_NOTIFICATION_SECRET", ""):
        assert verify_agora_signature(b'{"a":1}', None) is True


def test_signature_rejects_missing_header_when_secret_configured():
    with patch.object(settings, "AGORA_NOTIFICATION_SECRET", "a-real-secret"):
        assert verify_agora_signature(b'{"a":1}', None) is False


def test_signature_accepts_correct_hmac_and_rejects_tampered_body():
    secret = "a-real-secret"
    body = b'{"a":1}'
    correct = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with patch.object(settings, "AGORA_NOTIFICATION_SECRET", secret):
        assert verify_agora_signature(body, correct) is True
        assert verify_agora_signature(b'{"a":2}', correct) is False
        assert verify_agora_signature(body, "0" * 64) is False


def test_parse_gd_voice_events_extracts_final_human_turns_with_uid():
    payload = {"payload": {"channel": "gd-x", "turns": [
        {"role": "user", "content": "I think we should ship the MVP first.", "uid": 123456, "turn_id": "t1", "is_final": True},
        {"role": "assistant", "content": "Let's hear another view.", "uid": 998877, "turn_id": "t2"},
        {"role": "user", "content": "", "uid": 654321, "turn_id": "t3"},
    ]}}
    events = parse_gd_voice_events(payload)
    assert len(events) == 1
    assert events[0].rtc_uid == 123456
    assert events[0].turn_id == "t1"
    assert events[0].text == "I think we should ship the MVP first."
    assert events[0].is_final is True


def test_parse_gd_voice_events_skips_turns_with_no_resolvable_uid():
    payload = {"payload": {"channel": "gd-x", "turns": [
        {"role": "user", "content": "hello", "turn_id": "t1"},
    ]}}
    assert parse_gd_voice_events(payload) == []


def test_parse_gd_voice_events_handles_contents_shape_and_alternate_uid_keys():
    payload = {"payload": {"contents": [
        {"role": "user", "content": "we need more data", "speaker_uid": "55555", "turn_id": "t9", "is_final": True},
    ]}}
    events = parse_gd_voice_events(payload)
    assert len(events) == 1
    assert events[0].rtc_uid == 55555
