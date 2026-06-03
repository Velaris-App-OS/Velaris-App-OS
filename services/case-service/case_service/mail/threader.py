"""Email threading — Message-Id chain preferred, subject tag fallback."""
from __future__ import annotations
import difflib
import re
import uuid
from typing import Optional, TYPE_CHECKING, Sequence

from sqlalchemy import select, cast, String as SAString
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from case_service.db.models import CaseTypeModel, CaseInstanceModel as _CaseInstance


_SUBJECT_TAG_RE = re.compile(r"\[HELIX-([0-9a-fA-F]{8})\]", re.IGNORECASE)


def build_subject_tag(case_id: uuid.UUID) -> str:
    return f"[HELIX-{str(case_id)[:8]}]"


def extract_subject_tag(subject: str) -> Optional[str]:
    if not subject:
        return None
    m = _SUBJECT_TAG_RE.search(subject)
    return m.group(1).lower() if m else None


def build_message_id(case_id: Optional[uuid.UUID], domain: str | None = None) -> str:
    import os
    _domain = domain or os.getenv("HELIX_AUTH_EMAIL_DOMAIN", "local")
    nonce = uuid.uuid4().hex[:12]
    short = str(case_id)[:8] if case_id else "free"
    return f"<case-{short}-{nonce}@{_domain}>"


def build_references_chain(in_reply_to: Optional[str], existing_refs: list) -> list[str]:
    chain = list(existing_refs or [])
    if in_reply_to and in_reply_to not in chain:
        chain.append(in_reply_to)
    return chain


async def resolve_case_id_from_message(
    session: AsyncSession,
    *,
    in_reply_to: Optional[str],
    references: list[str] | None,
    subject: Optional[str],
) -> Optional[uuid.UUID]:
    """Match an inbound email to a case.

    Order: in_reply_to + references → subject tag.
    """
    from case_service.db.models import EmailMessageModel, CaseInstanceModel

    candidates: list[str] = []
    if in_reply_to:
        candidates.append(in_reply_to)
    candidates.extend(references or [])

    if candidates:
        q = select(EmailMessageModel).where(EmailMessageModel.message_id.in_(candidates))
        res = await session.execute(q)
        for m in res.scalars().all():
            if m.case_id is not None:
                return m.case_id

    short = extract_subject_tag(subject or "")
    if short:
        q2 = select(CaseInstanceModel).where(
            cast(CaseInstanceModel.id, SAString).ilike(f"{short}%")
        ).limit(2)
        res2 = await session.execute(q2)
        rows = list(res2.scalars().all())
        if len(rows) == 1:
            return rows[0].id
    return None


_OPEN_STATUSES = {"new", "open", "reopened"}
_FUZZY_THRESHOLD = 0.80
_STOPWORDS = frozenset({"a", "an", "the", "of", "for", "with", "to", "in", "on", "at", "and", "or", "is", "are"})
_SHORT_NAME_MAX_WORDS = 4  # names with <= this many words use all-words-present scoring


def _meaningful_words(text: str) -> list[str]:
    return [w for w in re.findall(r'\b\w+\b', text.lower()) if w not in _STOPWORDS and len(w) > 2]


def _score_candidate(text: str, candidate: str) -> float:
    """Score how well `candidate` (case type name or description) matches `text`.

    Short names (<=4 total words): fraction of all meaningful words present.
    Long names (>4 total words): 0.80 if first 2 meaningful words appear in text,
                                  else fraction of all meaningful words present.
    """
    cand = candidate.lower().strip()
    if not cand:
        return 0.0

    if cand in text:
        return 1.0

    all_words = re.findall(r'\b\w+\b', cand)
    mwords = _meaningful_words(cand)
    if not mwords:
        return 0.0

    if len(all_words) <= _SHORT_NAME_MAX_WORDS:
        # Short/specific names: all meaningful words must be present for a strong match
        present = sum(1 for w in mwords if re.search(rf'\b{re.escape(w)}\b', text))
        return present / len(mwords)
    else:
        # Long/verbose names: first 2 meaningful words form the "concept pair"
        concept = mwords[:2]
        if all(re.search(rf'\b{re.escape(w)}\b', text) for w in concept):
            return _FUZZY_THRESHOLD
        # Fallback: proportional word coverage
        present = sum(1 for w in mwords if re.search(rf'\b{re.escape(w)}\b', text))
        return present / len(mwords)


async def detect_case_types_from_content(
    session: AsyncSession,
    subject: str,
    body: str,
) -> list[tuple[float, "CaseTypeModel"]]:
    """Return all case types whose name/description matches subject+body at >= threshold.

    Returns list of (score, case_type) sorted by score DESC.
    """
    from case_service.db.models import CaseTypeModel

    text = f"{subject or ''} {body or ''}".lower()
    if not text.strip():
        return []

    q = select(CaseTypeModel).where(CaseTypeModel.is_deleted.is_(False))
    rows = (await session.execute(q)).scalars().all()

    results: dict[str, tuple[float, "CaseTypeModel"]] = {}
    for ct in rows:
        best = 0.0
        for candidate in filter(None, [ct.name, ct.description]):
            s = _score_candidate(text, candidate)
            if s > best:
                best = s
        if best >= _FUZZY_THRESHOLD:
            key = str(ct.id)
            if key not in results or best > results[key][0]:
                results[key] = (best, ct)

    # Sort by score DESC, then by name length ASC (shorter = more specific)
    return sorted(results.values(), key=lambda x: (-x[0], len(x[1].name)))


# Keep backward-compat alias used by service.py (returns best single match)
async def detect_case_type_from_content(
    session: AsyncSession,
    subject: str,
    body: str,
) -> Optional["CaseTypeModel"]:
    matches = await detect_case_types_from_content(session, subject, body)
    return matches[0][1] if matches else None


async def find_open_case_for_sender(
    session: AsyncSession,
    from_address: str,
    case_type_id: uuid.UUID,
) -> Optional["_CaseInstance"]:
    """Find the most recent open case of a given type submitted by this sender."""
    from case_service.db.models import CaseInstanceModel

    q = (
        select(CaseInstanceModel)
        .where(
            CaseInstanceModel.portal_submitter_email == from_address.lower().strip(),
            CaseInstanceModel.case_type_id == case_type_id,
            CaseInstanceModel.status.in_(_OPEN_STATUSES),
        )
        .order_by(CaseInstanceModel.created_at.desc())
        .limit(1)
    )
    return (await session.execute(q)).scalar_one_or_none()
