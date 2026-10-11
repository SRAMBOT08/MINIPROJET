from enum import Enum
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.intelligence.core.contracts import StrictModel


class GDConfiguration(StrictModel):
    title: str = Field(min_length=3,max_length=160)
    topic: str = Field(min_length=10,max_length=2000)
    duration_seconds: int = Field(default=1200,ge=60,le=3600)
    max_participants: int = Field(default=6,ge=2,le=12)
    min_participants: int = Field(default=2,ge=2,le=12)
    competencies: list[str] = Field(default_factory=lambda:["communication","argument_quality","collaboration","topic_relevance","listening","leadership"],min_length=1,max_length=8)
    policy_query: str = Field(default="",max_length=250)

    @model_validator(mode="after")
    def limits(self):
        if self.min_participants>self.max_participants: raise ValueError("Minimum participants exceeds capacity")
        if len(set(self.competencies))!=len(self.competencies) or any(not c or len(c)>80 for c in self.competencies): raise ValueError("Competencies must be unique and bounded")
        return self


class SessionCreate(StrictModel):
    configuration: GDConfiguration
    request_id: UUID


class InviteInput(StrictModel):
    candidate_id: str = Field(min_length=1,max_length=100)
    expires_minutes: int = Field(default=1440,ge=1,le=10080)
    request_id: UUID


class AcceptInvite(StrictModel):
    token: str = Field(min_length=40,max_length=100)
    request_id: UUID


class DiscussionMessage(StrictModel):
    text: str = Field(min_length=1,max_length=4000)
    reply_to: UUID|None = None
    request_id: UUID


class ParticipantAction(StrictModel):
    action: Literal["leave","rejoin","raise_hand","lower_hand"]
    request_id: UUID


class GDControl(StrictModel):
    action: Literal["start","finish","cancel"]
    expected_revision: int = Field(ge=0)
    request_id: UUID


class RemoveParticipant(StrictModel):
    request_id: UUID


class GDModeratorActionType(str,Enum):
    CONTINUE="CONTINUE"
    ENCOURAGE_PARTICIPATION="ENCOURAGE_PARTICIPATION"
    REDIRECT_DISCUSSION="REDIRECT_DISCUSSION"
    ASK_CLARIFICATION="ASK_CLARIFICATION"
    INTRODUCE_NEW_ANGLE="INTRODUCE_NEW_ANGLE"
    REQUEST_RESPONSE="REQUEST_RESPONSE"
    WARN_TIME="WARN_TIME"
    END_DISCUSSION="END_DISCUSSION"


class GDModeratorAction(StrictModel):
    action: GDModeratorActionType
    target_participant_id: str|None=None
    rationale: str = Field(min_length=1,max_length=500)


class VoiceTranscriptIn(StrictModel):
    """Normalized, speaker-attributed transcript segment from the Agora RTC channel.

    `rtc_uid` is the wire identity Agora reports for the speaker; the backend maps
    it back to a participant via `app.group_discussion.identity`, never trusting a
    client-supplied participant/candidate id directly. `turn_id` is Agora's own
    stable per-utterance identifier and is the sole idempotency key for commits —
    never a timestamp, so retried or out-of-order delivery of the same final turn
    is a no-op rather than a duplicate contribution.
    """
    rtc_uid: int = Field(ge=1)
    turn_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=0, max_length=4000)
    is_final: bool = True
    timestamp_ms: float | None = None
