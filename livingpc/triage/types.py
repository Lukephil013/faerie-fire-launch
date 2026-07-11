"""Structured triage outputs. Plain dataclasses so they're easy to build,
serialize, and test independent of any LLM."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Statement:
    """A brand-new candidate fact."""
    category: str
    attribute: str
    value: str
    confidence: float = 0.7
    note: str = ""


@dataclass
class Supersession:
    """A proposal to replace an existing active memory (by id)."""
    memory_id: int
    value: str
    attribute: str | None = None
    reason: str = ""
    confidence: float = 0.7


@dataclass
class Question:
    """A clarifying question for the user."""
    text: str
    category: str = ""


@dataclass
class TriageResult:
    statements: list[Statement] = field(default_factory=list)
    supersessions: list[Supersession] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "TriageResult":
        return cls(
            statements=[
                Statement(
                    category=s.get("category", ""),
                    attribute=s.get("attribute", ""),
                    value=s.get("value", ""),
                    confidence=float(s.get("confidence", 0.7)),
                    note=s.get("note", ""),
                )
                for s in d.get("statements", [])
            ],
            supersessions=[
                Supersession(
                    memory_id=int(s["memory_id"]),
                    value=s.get("value", ""),
                    attribute=s.get("attribute"),
                    reason=s.get("reason", ""),
                    confidence=float(s.get("confidence", 0.7)),
                )
                for s in d.get("supersessions", [])
                if "memory_id" in s
            ],
            questions=[
                Question(text=q.get("text", ""), category=q.get("category", ""))
                for q in d.get("questions", [])
            ],
        )

    def is_empty(self) -> bool:
        return not (self.statements or self.supersessions or self.questions)
