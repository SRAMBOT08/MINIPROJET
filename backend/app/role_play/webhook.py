"""Agora server-to-server notification adapter for Role Play voice turns.

A Role Play channel has exactly one candidate and one AI persona, so (unlike Group
Discussion) there is no cross-speaker attribution ambiguity to resolve -- this mirrors
the single-user Agent History / Turns Finished shape already consumed for Standard
Interview (app.transcript.service), just routed into the role_play feature's own
turn-commit path instead.
"""

from __future__ import annotations

from typing import Any

from app.group_discussion.webhook import verify_agora_signature  # shared HMAC verification

__all__ = ["verify_agora_signature", "parse_roleplay_voice_turns"]


def parse_roleplay_voice_turns(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize an Agora webhook payload into candidate voice turns: {turn_id, text}."""
    inner = payload.get("payload") or payload
    turns: list[dict[str, Any]] = []
    for idx, item in enumerate(inner.get("turns") or inner.get("contents") or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("speaker") or "").lower()
        if role in ("assistant", "agent"):
            continue  # the persona's own speech is not a candidate contribution
        text = str(item.get("content") or item.get("text") or "").strip()
        if not text or not item.get("is_final", True):
            continue
        turn_id = str(item.get("turn_id") or item.get("id") or f"{inner.get('channel', 'roleplay')}-{idx}")
        turns.append({"turn_id": turn_id, "text": text})
    return turns
