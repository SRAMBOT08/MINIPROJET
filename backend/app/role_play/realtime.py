"""Agora-aware realtime descriptor for Role Play sessions (see group_discussion/realtime.py
for the analogous Group Discussion adapter -- the two features intentionally share the same
shape so the frontend's realtime-status handling isn't feature-specific)."""

from __future__ import annotations

from app.core.config import settings


class AgoraRolePlayRealtimeAdapter:
    def describe(self, session_id: str) -> dict:
        configured = bool((settings.AGORA_APP_ID or "").strip() and (settings.AGORA_APP_CERTIFICATE or "").strip())
        if not configured:
            return {
                "session_id": session_id,
                "mode": "local_text",
                "voice_available": False,
                "message": "Agora is not configured on this server. Voice is unavailable; the simulation runs as text only.",
            }
        return {
            "session_id": session_id,
            "mode": "agora_rtc",
            "voice_available": True,
            "channel_name": f"roleplay-{session_id}",
            "message": "Realtime voice simulation. Join to speak with the character and hear their responses.",
        }
