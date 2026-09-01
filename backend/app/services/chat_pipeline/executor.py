from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

from app.services.chat_pipeline.plugin import ChatPipelineError, ChatPipelineRegistry
from app.services.chat_pipeline.types import ChatPipelineContext, ChatPipelineEvent, StageOutput


class ChatPipelineExecutor:
    def __init__(self, registry: ChatPipelineRegistry):
        self.registry = registry

    def run(self, context: ChatPipelineContext, stage_ids: Iterable[str]) -> Iterator[ChatPipelineEvent]:
        for stage_id in tuple(stage_ids):
            if context.runtime.is_stopped():
                context.state.stopped = True
                yield ChatPipelineEvent("stop", {"stop": {"reason": "client_requested", "stage_id": stage_id}})
                yield ChatPipelineEvent("done", {}, terminal=True)
                return
            plugin = None
            try:
                plugin = self.registry.get(stage_id)
                context.state.stage_progress.append(_progress(stage_id, "started"))
                output = plugin.run(context)
                yield from self._iter_output(output)
                context.state.stage_progress.append(_progress(stage_id, "completed"))
            except ChatPipelineError as exc:
                yield from self._fail(context, exc.stage_id, exc)
                return
            except Exception as exc:
                yield from self._fail(context, stage_id, ChatPipelineError(stage_id, str(exc), cause=exc))
                return
            finally:
                cleanup = getattr(plugin, "cleanup", None)
                if callable(cleanup):
                    cleanup(context)

    def _iter_output(self, output: StageOutput) -> Iterator[ChatPipelineEvent]:
        if output is None:
            return
        if isinstance(output, ChatPipelineEvent):
            yield output
            return
        if isinstance(output, Iterable):
            for item in output:
                if item is not None:
                    yield item

    def _fail(
        self,
        context: ChatPipelineContext,
        stage_id: str,
        error: ChatPipelineError,
    ) -> Iterator[ChatPipelineEvent]:
        context.state.failed_stage_id = stage_id
        context.state.error = str(error)
        context.state.stage_progress.append(_progress(stage_id, "failed", error_type=error.__class__.__name__, error=str(error)))
        yield ChatPipelineEvent(
            "error",
            {"error": str(error), "pipeline": {"stage_id": stage_id, "error_type": error.__class__.__name__}},
        )
        yield ChatPipelineEvent("done", {}, terminal=True)


def _progress(stage_id: str, status: str, **extra: object) -> dict[str, object]:
    return {
        "stage_id": stage_id,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        **extra,
    }
