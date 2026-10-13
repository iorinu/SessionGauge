from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Counters:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Session:
    id: str
    source: str | None
    model: str | None
    parent_session_id: str | None
    counters: Counters
    estimated_cost_usd: float | None
    actual_cost_usd: float | None
    started_at: float | None
    ended_at: float | None
    title: str | None = None
    cwd: str | None = None
    git_repo_root: str | None = None
    last_activity_at: float | None = None
    end_reason: str | None = None

    @property
    def total_tokens(self) -> int | None:
        return self.counters.total_tokens

    @property
    def input_tokens(self):
        return self.counters.input_tokens

    @property
    def output_tokens(self):
        return self.counters.output_tokens

    @property
    def cache_read_tokens(self):
        return self.counters.cache_read_tokens

    @property
    def cache_write_tokens(self):
        return self.counters.cache_write_tokens

    @property
    def reasoning_tokens(self):
        return self.counters.reasoning_tokens

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["total_tokens"] = self.total_tokens
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
        ):
            data[name] = getattr(self, name)
        return data


@dataclass(frozen=True)
class Pane:
    endpoint: str
    pane_id: str
    terminal_id: str | None
    agent: str | None
    agent_status: str | None
    cwd: str | None
    title: str | None
    session_id: str | None

    @property
    def key(self) -> tuple[str, str]:
        return self.endpoint, self.pane_id

    @property
    def generation_key(self) -> tuple[str, str, str | None]:
        return self.endpoint, self.pane_id, self.terminal_id


@dataclass(frozen=True)
class Match:
    pane: Pane
    profile: str | None
    session_id: str | None
    status: str
