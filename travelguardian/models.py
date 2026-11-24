"""Core data types. All datetimes are timezone-aware (local time of the airport/city)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Flight(BaseModel):
    id: str
    kind: Literal["flight"] = "flight"
    confirmation: str
    airline: str
    flight_number: str
    origin: str  # IATA airport code
    destination: str
    departs: datetime
    arrives: datetime
    passenger: str
    cabin: Optional[str] = None
    seat: Optional[str] = None
    source: Optional[str] = None  # email message id it was read from


class HotelStay(BaseModel):
    id: str
    kind: Literal["hotel"] = "hotel"
    confirmation: str
    name: str
    city: str
    address: Optional[str] = None
    check_in: date
    check_out: date
    check_in_time: str = "15:00"
    guest: str
    source: Optional[str] = None


class CalendarEvent(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    location: Optional[str] = None
    city: Optional[str] = None
    attendees: list[str] = Field(default_factory=list)


class Conflict(BaseModel):
    severity: Literal["info", "warning", "critical"]
    kind: str
    message: str
    items: list[str] = Field(default_factory=list)


class FlightStatus(BaseModel):
    flight_number: str
    status: Literal["on_time", "delayed", "cancelled"]
    scheduled_departure: datetime
    scheduled_arrival: datetime
    estimated_departure: datetime
    estimated_arrival: datetime
    delay_minutes: int = 0
    reason: Optional[str] = None


class Alternative(BaseModel):
    id: str
    airline: str
    flight_number: str
    origin: str
    destination: str
    departs: datetime
    arrives: datetime
    seats_available: int
    protected: bool = Field(
        description="True if the original carrier/alliance must rebook at no cost under irregular operations."
    )
    fare_difference: float = 0.0
    currency: str = "USD"
    notes: Optional[str] = None


class Draft(BaseModel):
    id: str
    kind: Literal["rebooking_request", "cancellation_request", "hotel_notice", "meeting_reschedule", "other"]
    to: str
    subject: str
    body: str
    created_at: datetime
    related: list[str] = Field(default_factory=list)
    status: Literal["draft"] = "draft"  # TravelGuardian never sends; a human does.


class EmailRef(BaseModel):
    """Where a booking was read from, so a rescheduling request can reply in the same thread."""

    provider: Literal["gmail", "local"]
    message_id: str  # provider id (Gmail message id, or inbox/<file>.eml)
    thread_id: str
    rfc_message_id: Optional[str] = None  # the Message-ID header, for In-Reply-To/References
    subject: str = ""
    sender: str = ""


class TranscriptTurn(BaseModel):
    role: Literal["agent", "user"]
    message: str
    time_in_call_secs: Optional[int] = None


class CallRecord(BaseModel):
    provider: Literal["elevenlabs", "simulated"]
    status: str  # initiated, in-progress, processing, done, failed (ElevenLabs conversation status)
    started_at: datetime
    to_number: Optional[str] = None
    conversation_id: Optional[str] = None
    call_sid: Optional[str] = None
    message: Optional[str] = None


class ProposedChange(BaseModel):
    id: str
    segment_ids: list[str]
    alternative_id: str
    reason: str
    created_at: datetime
    status: Literal["pending", "approved", "declined", "applied", "failed"] = "pending"
    decided_by: Optional[str] = None
    result: Optional[str] = None
    # --- voice approval workflow (all optional so older trip.json files still load)
    context: dict = Field(default_factory=dict)  # risk-engine facts the agent reads out
    rationale: Optional[str] = None  # why this alternative (LLM-written, rules-chosen)
    email_ref: Optional[EmailRef] = None  # confirmation email thread to reply in
    email_draft: Optional[dict] = None  # {"to", "subject", "body"} sent only after approval
    decided_at: Optional[datetime] = None
    new_segment_id: Optional[str] = None
    # Side effects done after approval, keyed by step ("email", "calendar"); replays skip done steps.
    actions: dict[str, dict] = Field(default_factory=dict)
    call: Optional[CallRecord] = None
    transcript: list[TranscriptTurn] = Field(default_factory=list)
    spoken_confirmation: Optional[str] = None
