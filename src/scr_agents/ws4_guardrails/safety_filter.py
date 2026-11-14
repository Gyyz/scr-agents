"""Safety filter: everything untrusted passes here before an agent sees it.

Untrusted = diff text, commit messages, PR text, scanner output, tool results, web
results. The filter never decides what is a bug; it strips invisible characters, redacts
secrets, flags prompt-injection patterns, caps size and wraps content in <untrusted>
tags so the model treats it as data rather than instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from scr_agents.config import SafetyConfig

INVISIBLE = re.compile(
    r"[\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]"
)

# (name, pattern, group holding the secret value or 0 for the whole match)
SECRET_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), 0),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), 0),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), 0),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"), 0),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), 0),
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z ]*PRIVATE KEY-----"
        ),
        0,
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        0,
    ),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), 0),
    ("openai_project_key", re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}\b"), 0),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0),
    # quoted value after a secret-looking key, in code, YAML or JSON: password = '...',
    # "client_secret": "..."
    (
        "assignment",
        re.compile(
            r"(?i)(?:api[_-]?key|apikey|secret|password|passwd|token|access[_-]?key|"
            r"private[_-]?key)[\"']?\s*[:=]\s*"
            r"[\"'](?!\[REDACTED:)([^\"'\s]{8,})[\"']"
        ),
        1,
    ),
    # env-file style line: DB_PASSWORD=value, export API_TOKEN=value (unquoted)
    (
        "env_secret",
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]*"
            r"(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|ACCESS_KEY|PRIVATE_KEY)"
            r"[ \t]*=[ \t]*(?![\"'])(?!\[REDACTED:)([^\s\"'#]{8,})"
        ),
        1,
    ),
    # user:password@host in URLs
    (
        "url_credentials",
        re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:(?!\[REDACTED:)([^@\s/]+)@"),
        1,
    ),
]

# Chat-template control tokens of common local models. Inside untrusted content they
# could end the turn or open a fake system turn; vLLM may tokenise them as real special
# tokens, so they are neutralised (angle brackets escaped) rather than merely flagged.
CONTROL_TOKENS = re.compile(
    r"<\|(?:im_start|im_end|eot_id|start_header_id|end_header_id|begin_of_text|"
    r"end_of_text|endoftext|system|user|assistant|tool_call|tool_response|end)\|>"
    r"|</?(?:start_of_turn|end_of_turn|bos|eos|think)>"
    r"|\[/?INST\]|<</?SYS>>"
)

INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override)\s+(?:\w+\s+){0,3}"
        r"(?:instructions?|prompts?|rules?|guidelines?)\b"
    ),
    re.compile(r"(?i)\byou\s+are\s+now\s+(?:a|an|the)\b"),
    re.compile(r"(?i)\bnew\s+instructions?\s*:"),
    re.compile(
        r"(?i)\b(?:approve|mark)\s+(?:this|the)\s+"
        r"(?:pr|pull request|change|patch|commit)"
        r"\s+(?:as\s+)?(?:safe|approved|clean|secure)"
    ),
    re.compile(r"(?i)\bdo\s+not\s+(?:report|flag|mention)\b"),
    re.compile(
        r"(?i)\b(?:ai|llm|assistant|reviewer|agent)s?\b[^.\n]{0,40}"
        r"\b(?:must|should|need to)\b"
        r"[^.\n]{0,40}\b(?:approve|skip|ignore|run|execute)\b"
    ),
    re.compile(r"(?i)\brun\s+(?:the\s+following|this)\s+command\b"),
    re.compile(r"(?i)curl\s+[^\n|]*\|\s*(?:ba)?sh\b"),
    re.compile(r"(?im)^\s*(?:assistant|system)\s*:"),
]

_FAKE_TAG = re.compile(r"(?i)</?\s*untrusted\b[^>]*>")


@dataclass
class Filtered:
    """Untrusted text after the safety filter, with what the filter found and did.

    ``flags`` name what was found, such as ``prompt_injection`` or
    ``secrets_redacted:2``; blocked content is replaced by a notice when wrapped.
    """

    text: str
    source: str
    flags: list[str] = field(default_factory=list)
    redactions: int = 0
    truncated: bool = False
    blocked: bool = False

    def wrapped(self) -> str:
        """Tagged form to place in a prompt.

        Content inside is data, never instructions.
        """
        attrs = f' source="{self.source}"'
        if self.flags:
            attrs += f' flags="{",".join(self.flags)}"'
        if self.blocked:
            return (
                f'<untrusted{attrs} blocked="true">'
                "[content removed by safety filter]</untrusted>"
            )
        return f"<untrusted{attrs}>\n{self.text}\n</untrusted>"


class SafetyFilter:
    """Clean untrusted text and wrap it in ``<untrusted>`` tags for a prompt.

    It never decides what is a bug: it removes or escapes what could steer the model,
    redacts secrets, flags prompt-injection patterns and caps the size.
    """

    def __init__(self, config: SafetyConfig | None = None):
        """Use ``config``, or the default safety settings when it is ``None``."""
        self.config = config or SafetyConfig()

    def sanitize(
        self, text: str | None, source: str, max_chars: int | None = None
    ) -> Filtered:
        """Clean ``text`` and report what was found, without wrapping it.

        Invisible characters are removed, fake ``<untrusted>`` tags and chat-template
        control tokens are escaped, secrets are redacted (with ``redact_secrets``),
        prompt-injection patterns are flagged and the text is cut to the length limit.
        With ``block_on_injection``, text flagged as prompt injection is marked blocked.

        Args:
            text: The untrusted text; ``None`` counts as empty.
            source: Where the text comes from, such as ``code_diff`` or ``web``.
            max_chars: Length limit; defaults to the configured ``max_chars``.

        Returns:
            The cleaned text with its flags, redaction count, and truncated and
            blocked states.
        """
        text = text or ""
        flags: list[str] = []
        redactions = 0

        text, n = INVISIBLE.subn("", text)
        if n:
            flags.append(f"invisible_chars:{n}")

        neutral = _FAKE_TAG.sub(
            lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text
        )
        if neutral != text:
            flags.append("fake_untrusted_tag")
            text = neutral

        text, n = CONTROL_TOKENS.subn(
            lambda m: (
                m.group(0)
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("[", "&#91;")
                .replace("]", "&#93;")
            ),
            text,
        )
        if n:
            flags.append(f"control_tokens:{n}")

        if self.config.redact_secrets:
            for name, pat, group in SECRET_PATTERNS:

                def _redact(
                    m: re.Match[str], _name: str = name, _group: int = group
                ) -> str:
                    if _group == 0:
                        return f"[REDACTED:{_name}]"
                    s, e = m.span(_group)
                    return (
                        m.group(0)[: s - m.start()]
                        + f"[REDACTED:{_name}]"
                        + m.group(0)[e - m.start() :]
                    )

                text, n = pat.subn(_redact, text)
                redactions += n
            if redactions:
                flags.append(f"secrets_redacted:{redactions}")

        if any(p.search(text) for p in INJECTION_PATTERNS):
            flags.append("prompt_injection")

        limit = max_chars or self.config.max_chars
        truncated = False
        if len(text) > limit:
            text = text[:limit] + f"\n...[truncated by safety filter at {limit} chars]"
            truncated = True
            flags.append("truncated")

        blocked = self.config.block_on_injection and "prompt_injection" in flags
        return Filtered(
            text=text,
            source=source,
            flags=flags,
            redactions=redactions,
            truncated=truncated,
            blocked=blocked,
        )

    def wrap(self, text: str | None, source: str, max_chars: int | None = None) -> str:
        """Clean ``text`` with ``sanitize`` and return its tagged form for a prompt."""
        return self.sanitize(text, source, max_chars).wrapped()
