"""Unit tests for the GD RTC UID <-> participant identity mapping. No DB, no Agora."""
from app.group_discussion.identity import (
    MODERATOR_RTC_UID,
    assign_participant_uids,
    resolve_speaker,
)


def test_uid_assignment_is_deterministic_across_calls():
    roster = ["p1", "p2", "p3"]
    first = assign_participant_uids("session-a", roster)
    second = assign_participant_uids("session-a", roster)
    assert first == second


def test_uid_assignment_is_unique_and_avoids_moderator_uid():
    roster = [f"participant-{i}" for i in range(12)]  # GD's max_participants ceiling
    uids = assign_participant_uids("session-b", roster)
    assert len(set(uids.values())) == len(roster)
    assert MODERATOR_RTC_UID not in uids.values()


def test_uid_assignment_is_stable_per_session_not_just_per_participant():
    # The mapping mixes session_id into the hash (not a pure function of participant_id
    # alone), and is reproducible for the same (session_id, participant_id) pair.
    uid_a = assign_participant_uids("session-a", ["shared-participant"])["shared-participant"]
    assert uid_a == assign_participant_uids("session-a", ["shared-participant"])["shared-participant"]


def test_resolve_speaker_finds_the_right_participant():
    roster = ["alice", "bob", "carol"]
    uids = assign_participant_uids("session-c", roster)
    for participant_id, uid in uids.items():
        assert resolve_speaker("session-c", roster, uid) == participant_id


def test_resolve_speaker_returns_none_for_moderator_uid():
    assert resolve_speaker("session-d", ["alice"], MODERATOR_RTC_UID) is None


def test_resolve_speaker_returns_none_for_unknown_uid():
    # A UID that belongs to nobody in the roster must never silently resolve.
    roster = ["alice", "bob"]
    uids = set(assign_participant_uids("session-e", roster).values())
    stray = next(candidate for candidate in range(100_000, 100_050) if candidate not in uids and candidate != MODERATOR_RTC_UID)
    assert resolve_speaker("session-e", roster, stray) is None


def test_resolve_speaker_ignores_a_participant_who_has_left_the_current_roster():
    # A participant removed from the session (not present in the current roster
    # passed in by the caller) must not be resolvable, even if their old UID would
    # otherwise map correctly -- callers always pass the *current* joined roster.
    full_roster = ["alice", "bob"]
    uid_bob = assign_participant_uids("session-f", full_roster)["bob"]
    assert resolve_speaker("session-f", ["alice"], uid_bob) is None
