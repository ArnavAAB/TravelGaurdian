"""Email provider interface: read flight confirmations, reply in their thread.

GmailProvider talks to the Gmail API (OAuth 2.0). LocalMailProvider is the simulated fallback:
it reads samples/inbox/*.eml and writes replies as .eml files into data/outbox/.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path
from typing import Any, Protocol

from .ingest import Message, message_from_bytes, read_inbox
from .models import EmailRef


class MailProvider(Protocol):
    name: str

    def fetch_confirmations(self, max_results: int = 25) -> list[Message]: ...

    def reply(self, ref: EmailRef, to: str, body: str) -> dict: ...


def reply_subject(original: str) -> str:
    return original if original.lower().startswith("re:") else f"Re: {original}"


def build_reply(ref: EmailRef, to: str, body: str, sender: str = "") -> EmailMessage:
    em = EmailMessage()
    em["To"] = to
    if sender:
        em["From"] = sender
    em["Subject"] = reply_subject(ref.subject or "Flight booking")
    if ref.rfc_message_id:
        # These two headers (plus the Gmail threadId) keep the reply in the original conversation.
        em["In-Reply-To"] = ref.rfc_message_id
        em["References"] = ref.rfc_message_id
    em["Date"] = format_datetime(datetime.now(timezone.utc))
    em["Message-ID"] = make_msgid(domain="travelguardian.local")
    em.set_content(body)
    return em


def ref_for(msg: Message, provider: str) -> EmailRef:
    return EmailRef(
        provider=provider,  # type: ignore[arg-type]
        message_id=msg.id,
        thread_id=msg.thread_id,
        rfc_message_id=msg.rfc_message_id or None,
        subject=msg.subject,
        sender=msg.sender,
    )


class GmailProvider:
    name = "gmail"

    def __init__(self, service: Any, query: str):
        self.svc = service
        self.query = query

    def fetch_confirmations(self, max_results: int = 25) -> list[Message]:
        listing = self.svc.users().messages().list(userId="me", q=self.query, maxResults=max_results).execute()
        out = []
        for item in listing.get("messages", []):
            full = self.svc.users().messages().get(userId="me", id=item["id"], format="raw").execute()
            raw = base64.urlsafe_b64decode(full["raw"].encode())
            out.append(message_from_bytes(raw, f"gmail:{item['id']}", thread_id=full.get("threadId", "")))
        return out

    def reply(self, ref: EmailRef, to: str, body: str) -> dict:
        em = build_reply(ref, to, body)
        raw = base64.urlsafe_b64encode(em.as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        if ref.provider == "gmail" and ref.thread_id:
            payload["threadId"] = ref.thread_id
        sent = self.svc.users().messages().send(userId="me", body=payload).execute()
        return {"provider": "gmail", "id": sent.get("id"), "thread_id": sent.get("threadId"), "to": to,
                "subject": em["Subject"]}


class LocalMailProvider:
    name = "local"

    def __init__(self, inbox: Path, outbox: Path):
        self.inbox = inbox
        self.outbox = outbox

    def fetch_confirmations(self, max_results: int = 25) -> list[Message]:
        return read_inbox(self.inbox)[:max_results]

    def reply(self, ref: EmailRef, to: str, body: str) -> dict:
        em = build_reply(ref, to, body, sender="travelguardian@localhost")
        self.outbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        path = self.outbox / f"{stamp}-reply.eml"
        path.write_bytes(em.as_bytes())
        return {"provider": "local", "id": path.name, "thread_id": ref.thread_id, "to": to,
                "subject": em["Subject"], "path": str(path)}


__all__ = ["MailProvider", "GmailProvider", "LocalMailProvider", "build_reply", "ref_for", "reply_subject"]
