"""Stable RTC UID <-> participant identity mapping for Group Discussion voice sessions.

A GD channel holds one AI moderator plus N human participants. Agora RTC needs a
small positive integer UID per publisher in the channel; the backend must be able to
resolve "who is speaking" (an RTC UID reported by Agora) back to a logical
`participant_id` / `candidate_id` without trusting anything the client claims about
its own identity. UIDs are derived deterministically from `(session_id, participant_id)`
rather than stored, so no schema migration or extra write path is needed to keep the
mapping consistent — the same participant always resolves to the same UID for the
life of a session, including across backend restarts.
"""

from __future__ import annotations

import zlib

# Reserved for the AI moderator agent (see app.agents.gd_mod.get_gd_mod_agora_mapping).
# Never assigned to a human participant.
MODERATOR_RTC_UID = 998877

_UID_RANGE_START = 100_000
_UID_RANGE_END = 999_999  # exclusive; stays clear of the moderator's reserved UID above it


def _base_uid(session_id: str, participant_id: str) -> int:
    digest = zlib.crc32(f"{session_id}:{participant_id}".encode("utf-8"))
    span = _UID_RANGE_END - _UID_RANGE_START
    return _UID_RANGE_START + (digest % span)


def assign_participant_uids(session_id: str, participant_ids: list[str]) -> dict[str, int]:
    """Deterministically assign a unique RTC UID to each participant in a session.

    Collisions (with the moderator's reserved UID, or between two participants'
    base hashes) are exceedingly rare but resolved deterministically by linear
    probing in participant order, so the mapping is still 100% reproducible from
    the same ordered participant list rather than depending on insertion timing.
    """
    assigned: dict[str, int] = {}
    used: set[int] = {MODERATOR_RTC_UID}
    for participant_id in participant_ids:
        candidate = _base_uid(session_id, participant_id)
        while candidate in used:
            candidate = _UID_RANGE_START + ((candidate - _UID_RANGE_START + 1) % (_UID_RANGE_END - _UID_RANGE_START))
        assigned[participant_id] = candidate
        used.add(candidate)
    return assigned


def participant_rtc_uid(session_id: str, participant_id: str, roster: list[str]) -> int:
    """Resolve one participant's RTC UID given the full ordered roster of the session.

    The full roster is required (not just the one participant_id) so that a
    collision resolved by probing stays consistent regardless of which
    participant happens to be looked up first.
    """
    return assign_participant_uids(session_id, roster)[participant_id]


def resolve_speaker(session_id: str, roster: list[str], rtc_uid: int) -> str | None:
    """Reverse-lookup: given an RTC UID observed on the wire, find its participant_id.

    Returns None if the UID belongs to the moderator or matches no current
    participant (e.g. a stale/removed participant, or a spoofed UID).
    """
    if rtc_uid == MODERATOR_RTC_UID:
        return None
    mapping = assign_participant_uids(session_id, roster)
    for participant_id, uid in mapping.items():
        if uid == rtc_uid:
            return participant_id
    return None
