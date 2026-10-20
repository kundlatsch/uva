"""Steering: a message that arrives while the agent is between lookup rounds.

While the agent loop reads what its lookups found and is about to call the LLM
again, the box is open: a new plain message from the user goes into it instead
of waiting for a turn of its own, and the agent adds it to the next LLM call
("he added, while you were working: ..."). That message then counts as handled
(it is not routed again). Bot process only, one conversation.
"""

from __future__ import annotations


class SteerBox:
    def __init__(self) -> None:
        self.open = False
        self._texts: list[str] = []
        self._labels: list[str] = []

    def begin(self) -> None:
        """The agent is between rounds: messages may join the next call."""
        self.open = True

    def take(self) -> tuple[list[str], list[str]]:
        """Close the box and hand over (texts, labels) that arrived meanwhile."""
        self.open = False
        texts, labels, self._texts, self._labels = self._texts, self._labels, [], []
        return texts, labels

    def offer(self, text: str, label: str = "") -> bool:
        """True when the message joined the agent's next call (and must not be routed)."""
        if not self.open or not text.strip() or text.lstrip().startswith("/"):
            return False
        self._texts.append(text.strip())
        self._labels.append(label or text.strip())
        return True
