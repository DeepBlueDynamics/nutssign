"""Envelope data model. One envelope = one document + its signers + its fields + an audit log."""
from __future__ import annotations

import datetime as dt
import secrets
from typing import List, Optional

from pydantic import BaseModel, Field


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def new_id(nbytes: int = 8) -> str:
    return secrets.token_urlsafe(nbytes)


class Signer(BaseModel):
    id: str = Field(default_factory=lambda: new_id(6))
    name: str
    email: str
    role: str = ""
    order: int = 1
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    status: str = "pending"  # pending | sent | signed
    sent_at: Optional[str] = None
    viewed_at: Optional[str] = None
    signed_at: Optional[str] = None
    consent_at: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    signature_kind: Optional[str] = None  # drawn | typed
    typed_name: Optional[str] = None


class SigField(BaseModel):
    id: str = Field(default_factory=lambda: new_id(6))
    signer_id: str
    kind: str = "signature"  # signature | date | initials | text | checkbox
    page: int  # 1-based
    x: float  # PDF points, top-left origin
    y: float
    w: float
    h: float
    value: Optional[str] = None
    group: Optional[str] = None  # checkboxes sharing a group act as one choice ("is" / "is not")
    required: bool = False  # a required checkbox group must have one box marked before signing


class AuditEvent(BaseModel):
    ts: str = Field(default_factory=now_iso)
    event: str
    signer_id: Optional[str] = None
    ip: Optional[str] = None
    detail: Optional[str] = None


class Envelope(BaseModel):
    id: str = Field(default_factory=lambda: new_id(9))
    admin_token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))  # private management link
    creator_email: str = ""
    title: str
    message: str = ""
    created_at: str = Field(default_factory=now_iso)
    status: str = "draft"  # draft | sent | completed | voided
    sequential: bool = True
    page_count: int = 0
    page_sizes: List[List[float]] = []  # [[width_pt, height_pt], ...]
    source_sha256: str = ""
    signed_sha256: Optional[str] = None
    sent_at: Optional[str] = None
    completed_at: Optional[str] = None
    signers: List[Signer] = []
    fields: List[SigField] = []
    audit: List[AuditEvent] = []

    # ----- lookups
    def signer(self, signer_id: str) -> Optional[Signer]:
        return next((s for s in self.signers if s.id == signer_id), None)

    def signer_by_token(self, token: str) -> Optional[Signer]:
        return next((s for s in self.signers if secrets.compare_digest(s.token, token)), None)

    def fields_for(self, signer_id: str) -> List[SigField]:
        return [f for f in self.fields if f.signer_id == signer_id]

    def field(self, field_id: str) -> Optional[SigField]:
        return next((f for f in self.fields if f.id == field_id), None)

    # ----- workflow
    def unsigned(self) -> List[Signer]:
        return [s for s in self.signers if s.status != "signed"]

    def current_order(self) -> Optional[int]:
        pending = self.unsigned()
        return min(s.order for s in pending) if pending else None

    def is_turn(self, signer: Signer) -> bool:
        if signer.status == "signed" or self.status != "sent":
            return False
        if not self.sequential:
            return True
        return signer.order == self.current_order()

    def signers_due(self) -> List[Signer]:
        """Signers who should have a live signing link right now."""
        return [s for s in self.signers if self.is_turn(s)]

    def all_signed(self) -> bool:
        return bool(self.signers) and not self.unsigned()

    def log(self, event: str, **kw) -> None:
        self.audit.append(AuditEvent(event=event, **kw))
