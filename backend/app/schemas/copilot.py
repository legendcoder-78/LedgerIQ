import uuid
from typing import Any

from pydantic import BaseModel


class CopilotRequest(BaseModel):
    batch_id: uuid.UUID
    message: str | None = None
    query: str | None = None


class CopilotSource(BaseModel):
    """A referenced exception or record that backs the copilot's answer."""
    type: str           # 'exception' | 'record' | 'batch_metric'
    id: str
    label: str
    amount: float | None = None
    risk_score: str | None = None
    identifier: str | None = None   # UI alias for `id`


class CopilotResponse(BaseModel):
    reply: str
    sources: list[CopilotSource] = []
    llm_used: bool = True
    model: str | None = None
