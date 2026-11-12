"""Bounded model-written context, separate from authoritative workflow status."""
import re
from pydantic import BaseModel, ConfigDict, Field
from .redaction import scrub_text


class SlackMessage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    headline: str = Field(min_length=1, max_length=80, description='Short, natural headline using human names, not internal IDs.')
    context: str = Field(min_length=1, max_length=260, description='One or two plain-language sentences explaining the observed situation before remediation.')
    impact: str = Field(min_length=1, max_length=220, description='A brief explanation of why it matters; distinguish possible exposure from observed use.')


INTERNAL_ID = re.compile(r'\b(?:emp|agent|service|grant|finding|app)[-:]\w|\bcustomers:\w|\b[0-9a-f]{8}-[0-9a-f]{4}-', re.I)
ACTION_CLAIM = re.compile(r'\b(?:I|we|Awasi)\s+(?:(?:have|has|already|successfully)\s+)*(?:paused|revoked|disabled|restricted|removed|restored|fixed|resolved|verified|resumed|approved|blocked)\b', re.I)


def clean_message(value):
    """Bad presentation falls back without discarding a valid investigation.

    These checks catch formatting, IDs and obvious action claims; they are not a
    general fact checker. Grounding still depends on the investigation evidence.
    """
    if value is None:
        return None
    try:
        raw = value.model_dump() if isinstance(value, SlackMessage) else value
        message = SlackMessage.model_validate(raw)
        cleaned = {key: scrub_text(text).strip() for key,text in message.model_dump().items()}
        for text in cleaned.values():
            if not text or any(c in text for c in '\n\r<>*`') or INTERNAL_ID.search(text) or ACTION_CLAIM.search(text):
                return None
        return SlackMessage.model_validate(cleaned).model_dump()
    except (ValueError, TypeError):
        return None
