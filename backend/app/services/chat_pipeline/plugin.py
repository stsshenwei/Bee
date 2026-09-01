from __future__ import annotations

from typing import Protocol

from app.services.chat_pipeline.types import ChatPipelineContext, StageOutput


class ChatPipelineError(RuntimeError):
    def __init__(self, stage_id: str, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.stage_id = stage_id
        self.cause = cause


class ChatPipelinePlugin(Protocol):
    stage_id: str

    def run(self, context: ChatPipelineContext) -> StageOutput:
        ...


class ChatPipelineRegistry:
    def __init__(self):
        self._plugins: dict[str, ChatPipelinePlugin] = {}

    def register(self, plugin: ChatPipelinePlugin) -> None:
        self._plugins[str(plugin.stage_id)] = plugin

    def get(self, stage_id: str) -> ChatPipelinePlugin:
        try:
            return self._plugins[stage_id]
        except KeyError as exc:
            raise ChatPipelineError(stage_id, f"Chat pipeline stage is not registered: {stage_id}") from exc

    def stage_ids(self) -> tuple[str, ...]:
        return tuple(self._plugins)
