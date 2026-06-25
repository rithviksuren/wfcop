from __future__ import annotations

import re


EMAIL_NOUN_PATTERN = r"(?:e-?mails?|mails?|gmail|inbox|messages?)"
EMAIL_REFERENCE_PATTERN = re.compile(
    rf"\b{EMAIL_NOUN_PATTERN}\b",
    re.I,
)

_ACTION_BOUNDARY = (
    r"(?:create|send|make|add|save|copy|store|post|notify|update|"
    r"archive|forward|publish|set|remind)"
)


def has_email_reference(value: str) -> bool:
    return bool(EMAIL_REFERENCE_PATTERN.search(value))


def extract_email_search_text(value: str) -> str:
    """Extract the subject/body text an email workflow should search for.

    Natural category language such as "job-related emails" means emails whose
    text contains "job". Explicitly quoted phrases remain exact.
    """

    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""

    quoted_patterns = (
        r"""(?:word|phrase)\s+["']([^"']+)["']""",
        r"""(?:containing|contains|with|about|regarding|related\s+to|for)\s+["']([^"']+)["']""",
    )
    for pattern in quoted_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _normalize_search_value(match.group(1))

    word_match = re.search(
        r"\bwith\s+(?:the\s+)?word\s+([A-Za-z0-9_+-]+)",
        text,
        re.I,
    )
    if word_match:
        return _normalize_search_value(word_match.group(1))

    category_match = re.search(
        rf"(?P<topic>.+?)\s*[- ]related\s+{EMAIL_NOUN_PATTERN}\b",
        text,
        re.I,
    )
    if category_match:
        return _normalize_topic(category_match.group("topic"))

    patterns = (
        rf"\b{EMAIL_NOUN_PATTERN}\s+(?:that\s+(?:is|are)\s+)?"
        rf"(?:related\s+to|about|regarding)\s+(.+)$",
        rf"\b{EMAIL_NOUN_PATTERN}\s+for\s+"
        rf"(?!{EMAIL_NOUN_PATTERN}\b)(.+)$",
        rf"\b{EMAIL_NOUN_PATTERN}\s+(?:containing|contains)\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return _normalize_search_value(_trim_action_clause(match.group(1)))
    return ""


def _normalize_topic(value: str) -> str:
    topic = _trim_action_clause(value)
    connector = re.search(
        r"\b(?:containing|contains|with(?:\s+(?:the\s+)?word)?|for)\s+(.+)$",
        topic,
        re.I,
    )
    if connector:
        topic = connector.group(1)

    topic = re.sub(r"^(?:whenever|when|if)\s+", "", topic, flags=re.I)
    topic = re.sub(
        r"^(?:(?:i|we|you)\s+)?"
        r"(?:get|receive|received|find|check|search|monitor|watch|read|see|have)\s+",
        "",
        topic,
        flags=re.I,
    )
    topic = re.sub(
        rf"^(?:(?:my|the)\s+)?(?:gmail|inbox|{EMAIL_NOUN_PATTERN})\s+(?:for\s+)?",
        "",
        topic,
        flags=re.I,
    )
    topic = re.sub(
        r"^(?:(?:a|an|the|all|any|some|my|new|unread|incoming)\s+)+",
        "",
        topic,
        flags=re.I,
    )
    return _normalize_search_value(topic)


def _trim_action_clause(value: str) -> str:
    return re.split(
        rf"\s*(?:,|;)\s*|\s+and\s+(?={_ACTION_BOUNDARY}\b)",
        value,
        maxsplit=1,
        flags=re.I,
    )[0]


def _normalize_search_value(value: str) -> str:
    clean = re.sub(r"\s+", " ", value).strip(" \t\r\n.,;:!?\"'")
    if clean.lower() in {
        "newletter",
        "newletters",
        "newsletter",
        "newsletters",
    }:
        return "newsletter"
    return clean
