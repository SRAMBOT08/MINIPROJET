"""Agora-aware realtime descriptor for Group Discussion sessions.

`describe()` is surfaced verbatim to the frontend (`GDService.view()` ->
`realtime` key) so the UI can tell a genuine voice-capable deployment from one
where Agora simply isn't configured (e.g. a local dev checkout with blank
`AGORA_APP_ID`/`AGORA_APP_CERTIFICATE`). It intentionally never claims voice is
available unless the server can actually mint RTC credentials.
"""

from __future__ import annotations

from app.core.config import settings


class AgoraGDRealtimeAdapter:
    def describe(self, session_id: str) -> dict:
        configured = bool((settings.AGORA_APP_ID or "").strip() and (settings.AGORA_APP_CERTIFICATE or "").strip())
        if not configured:
            return {
                "session_id": session_id,
                "mode": "local_text",
                "voice_available": False,
                "message": "Agora is not configured on this server. Voice is unavailable; the discussion runs as text only.",
            }
        return {
            "session_id": session_id,
            "mode": "agora_rtc",
            "voice_available": True,
            "channel_name": f"gd-{session_id}",
            "message": "Realtime voice discussion. Join to publish your microphone and hear other participants and the AI moderator.",
        }
