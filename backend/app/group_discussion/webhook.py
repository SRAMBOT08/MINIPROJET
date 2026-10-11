"""Agora server-to-server notification adapter for Group Discussion voice turns.

Speaker attribution for GD MUST come from Agora's own ASR pipeline (the party that
actually knows which RTC UID produced which audio), not from a browser client
relaying what it believes it heard -- any authenticated participant's client could
otherwise forge a "final" contribution attributed to someone else's UID. This module
only adapts Agora's server-pushed webhook payload (mirroring the same Agent
History / Turns Finished events already consumed for Standard Interview in
app.transcript.service, but generalized to carry a per-turn speaker UID for a
multi-participant channel) into the app's own `VoiceTranscriptIn` shape.

KNOWN LIMITATION (see docs/GD_ROLEPLAY_ROOT_CAUSE_REPORT.md): the exact field Agora
uses to carry a per-turn speaker UID in a multi-human-participant Conversational AI
channel has not been observed against live multi-party traffic in this environment.
This parser tries every documented/plausible field name and falls back to the
channel's sole non-moderator UID when there is exactly one human participant, but a
production rollout MUST confirm the real field name against a live 2+ participant
session before this is relied upon for grading-grade speaker attribution.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from app.core.config import settings
from app.group_discussion.models import VoiceTranscriptIn

_UID_KEYS = ("uid", "user_id", "speaker_uid", "stream_id", "rtc_uid")


def verify_agora_signature(raw_body: bytes, header_value: str | None) -> bool:
    """Verify Agora's `Agora-Signature-V2` header (HMAC-SHA256 over the raw body).

    Returns True (and logs nothing) when `AGORA_NOTIFICATION_SECRET` is unset, so a
    local/dev deployment without the secret configured keeps working -- matching this
    codebase's existing degrade-gracefully posture for optional secrets elsewhere
    (e.g. GD_INVITATION_SECRET falling back to JWT_SECRET). Any deployment that DOES
    configure the secret gets real verification.
    """
    secret = (settings.AGORA_NOTIFICATION_SECRET or "").strip()
    if not secret:
        return True
    if not header_value:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value.strip().lower())


def _extract_uid(item: dict[str, Any]) -> int | None:
    for key in _UID_KEYS:
        value = item.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def parse_gd_voice_events(payload: dict[str, Any]) -> list[VoiceTranscriptIn]:
    """Normalize an Agora GD webhook payload into zero or more voice turns."""
    inner = payload.get("payload") or payload
    events: list[VoiceTranscriptIn] = []

    for idx, item in enumerate(inner.get("turns") or inner.get("contents") or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("speaker") or "").lower()
        if role in ("assistant", "agent"):
            continue  # the moderator's own speech is not a discussion contribution
        text = str(item.get("content") or item.get("text") or "").strip()
        if not text:
            continue
        uid = _extract_uid(item)
        if uid is None:
            continue
        turn_id = str(item.get("turn_id") or item.get("id") or f"{inner.get('channel', 'gd')}-{idx}")
        events.append(VoiceTranscriptIn(
            rtc_uid=uid,
            turn_id=turn_id,
            text=text,
            is_final=bool(item.get("is_final", True)),
            timestamp_ms=item.get("speech_start_ms") or item.get("timestamp"),
        ))
    return events
