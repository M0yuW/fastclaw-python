"""User instructions scoped to one root execution; accessed on the runtime event loop."""

from dataclasses import dataclass, field


@dataclass
class TaskSteering:
    owner: str
    agent_id: str
    updates: dict[str, tuple[int, str]] = field(default_factory=dict)
    closed: bool = False

    def add(self, request_id: str, text: str) -> int:
        previous = self.updates.get(request_id)
        if previous:
            if previous[1] != text:
                raise ValueError("Request ID already used for different text")
            return previous[0]
        if self.closed:
            raise ValueError("Task has finished")
        revision = len(self.updates) + 1
        self.updates[request_id] = (revision, text)
        return revision
