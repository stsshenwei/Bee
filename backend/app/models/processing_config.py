from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


PROCESSING_VERSION = "weknora-adaptive-v1"


class ParserErrorCode(str, Enum):
    PARSER_FAILED = "PARSER_FAILED"
    PARSER_ENGINE_UNAVAILABLE = "PARSER_ENGINE_UNAVAILABLE"
    PARSER_ENGINE_FAILED = "PARSER_ENGINE_FAILED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    PDF_PASSWORD_REQUIRED = "PDF_PASSWORD_REQUIRED"
    PDF_OPEN_FAILED = "PDF_OPEN_FAILED"
    PDF_PAGE_LIMIT_EXCEEDED = "PDF_PAGE_LIMIT_EXCEEDED"


class ChunkStrategy(str, Enum):
    AUTO = "auto"
    HEADING = "heading"
    HEURISTIC = "heuristic"
    RECURSIVE = "recursive"
    LEGACY = "legacy"


class ChunkType(str, Enum):
    PARENT = "parent"
    CHILD = "child"
    TABLE = "table"
    OCR = "ocr"
    IMAGE_OCR = "image_ocr"
    IMAGE_CAPTION = "image_caption"
    SUMMARY = "summary"


@dataclass(frozen=True)
class ProcessingRuntimeDefaults:
    parser_engine: str = "builtin"
    pdf_force_scanned: bool = False
    pdf_render_dpi: int = 200
    pdf_jpeg_quality: int = 90
    pdf_max_pages: int = 1000
    pdf_max_image_edge_px: int = 2400
    pdf_render_concurrency: int = 2
    chunk_strategy: str = ChunkStrategy.AUTO.value
    parent_child_enabled: bool = True
    parent_chunk_size_chars: int = 4096
    child_chunk_size_chars: int = 384
    child_chunk_overlap_chars: int = 76
    max_protected_span_chars: int = 7500
    embedding_token_limit: int = 0
    media_storage_provider: str = "local"
    media_storage_dir: str = "./vector_db/media"
    media_max_bytes: int = 25 * 1024 * 1024
    preview_max_file_bytes: int = 10 * 1024 * 1024
    preview_max_pages: int = 20
    preview_timeout_seconds: float = 5.0
    preview_max_chunks: int = 50
    ocr_enabled: bool = False
    ocr_provider: str = "disabled"
    ocr_min_confidence: float = 0.0
    caption_enabled: bool = False
    caption_provider: str = "disabled"
    dense_enabled: bool = True
    keyword_enabled: bool = True
    graph_enabled: bool = False
    wiki_enabled: bool = False
    question_generation_enabled: bool = False


@dataclass(frozen=True)
class DurableProcessingWorkerConfig:
    enabled: bool = False
    poll_interval_seconds: float = 1.0
    lease_timeout_seconds: int = 300
    max_concurrent_tasks: int = 1
    default_max_attempts: int = 3
    retry_backoff_seconds: tuple[int, ...] = (10, 30, 120)
    parser_max_attempts: int = 3
    chunk_max_attempts: int = 2
    embedding_max_attempts: int = 3
    multimodal_max_attempts: int = 3
    postprocess_max_attempts: int = 2

    @classmethod
    def from_settings(cls, settings: dict[str, Any] | None = None) -> "DurableProcessingWorkerConfig":
        raw = settings or {}
        return cls(
            enabled=_bool(raw.get("enabled"), cls.enabled),
            poll_interval_seconds=max(0.1, _float(raw.get("poll_interval_seconds"), cls.poll_interval_seconds)),
            lease_timeout_seconds=max(1, _int(raw.get("lease_timeout_seconds"), cls.lease_timeout_seconds)),
            max_concurrent_tasks=max(1, _int(raw.get("max_concurrent_tasks"), cls.max_concurrent_tasks)),
            default_max_attempts=max(1, _int(raw.get("default_max_attempts"), cls.default_max_attempts)),
            retry_backoff_seconds=_backoff_tuple(raw.get("retry_backoff_seconds"), cls.retry_backoff_seconds),
            parser_max_attempts=max(1, _int(raw.get("parser_max_attempts"), cls.parser_max_attempts)),
            chunk_max_attempts=max(1, _int(raw.get("chunk_max_attempts"), cls.chunk_max_attempts)),
            embedding_max_attempts=max(1, _int(raw.get("embedding_max_attempts"), cls.embedding_max_attempts)),
            multimodal_max_attempts=max(1, _int(raw.get("multimodal_max_attempts"), cls.multimodal_max_attempts)),
            postprocess_max_attempts=max(1, _int(raw.get("postprocess_max_attempts"), cls.postprocess_max_attempts)),
        )

    def max_attempts_for_stage(self, stage: str) -> int:
        stage = str(stage or "").strip().lower()
        return {
            "parse": self.parser_max_attempts,
            "parser": self.parser_max_attempts,
            "chunk": self.chunk_max_attempts,
            "chunking": self.chunk_max_attempts,
            "index": self.embedding_max_attempts,
            "embedding": self.embedding_max_attempts,
            "multimodal": self.multimodal_max_attempts,
            "postprocess": self.postprocess_max_attempts,
        }.get(stage, self.default_max_attempts)

    def retry_delay_for_attempt(self, attempt: int) -> int:
        if not self.retry_backoff_seconds:
            return 0
        index = max(0, min(len(self.retry_backoff_seconds) - 1, int(attempt) - 1))
        return int(self.retry_backoff_seconds[index])


@dataclass(frozen=True)
class ProcessingRequestedConfig:
    parser_engine: str = "builtin"
    pdf_force_scanned: bool = False
    pdf_render_dpi: int = 200
    pdf_jpeg_quality: int = 90
    pdf_max_pages: int = 1000
    pdf_max_image_edge_px: int = 2400
    pdf_render_concurrency: int = 2
    chunk_strategy: str = ChunkStrategy.AUTO.value
    size_unit: str = "chars"
    parent_child_enabled: bool = True
    parent_chunk_size_chars: int = 4096
    child_chunk_size_chars: int = 384
    child_chunk_overlap_chars: int = 76
    max_protected_span_chars: int = 7500
    embedding_token_limit: int = 0
    media_storage_provider: str = "local"
    media_storage_dir: str = "./vector_db/media"
    media_max_bytes: int = 25 * 1024 * 1024
    preview_max_file_bytes: int = 10 * 1024 * 1024
    preview_max_pages: int = 20
    preview_timeout_seconds: float = 5.0
    preview_max_chunks: int = 50
    ocr_enabled: bool = False
    ocr_provider: str = "disabled"
    ocr_min_confidence: float = 0.0
    caption_enabled: bool = False
    caption_provider: str = "disabled"
    dense_enabled: bool = True
    keyword_enabled: bool = True
    graph_enabled: bool = False
    wiki_enabled: bool = False
    question_generation_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProcessingEffectiveConfig(ProcessingRequestedConfig):
    processing_version: str = PROCESSING_VERSION
    inactive_overrides: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["inactive_overrides"] = list(self.inactive_overrides)
        data["warnings"] = list(self.warnings)
        return data


@dataclass(frozen=True)
class ResolvedProcessingConfig:
    requested: ProcessingRequestedConfig
    effective: ProcessingEffectiveConfig

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested.to_dict(),
            "effective": self.effective.to_dict(),
        }


def resolve_processing_config(
    settings: dict[str, Any] | None,
    defaults: ProcessingRuntimeDefaults,
    *,
    available_parser_engines: set[str] | None = None,
    caption_available: bool = False,
) -> ResolvedProcessingConfig:
    raw = dict(settings or {})
    requested = ProcessingRequestedConfig(
        parser_engine=_text(raw.get("parser_engine"), defaults.parser_engine),
        pdf_force_scanned=_bool(raw.get("pdf_force_scanned"), defaults.pdf_force_scanned),
        pdf_render_dpi=_int(raw.get("pdf_render_dpi"), defaults.pdf_render_dpi),
        pdf_jpeg_quality=_int(raw.get("pdf_jpeg_quality"), defaults.pdf_jpeg_quality),
        pdf_max_pages=_int(raw.get("pdf_max_pages"), defaults.pdf_max_pages),
        pdf_max_image_edge_px=_int(raw.get("pdf_max_image_edge_px"), defaults.pdf_max_image_edge_px),
        pdf_render_concurrency=_int(raw.get("pdf_render_concurrency"), defaults.pdf_render_concurrency),
        chunk_strategy=_text(raw.get("chunk_strategy"), defaults.chunk_strategy),
        parent_child_enabled=_bool(raw.get("parent_child_enabled"), defaults.parent_child_enabled),
        parent_chunk_size_chars=_int(raw.get("parent_chunk_size_chars"), defaults.parent_chunk_size_chars),
        child_chunk_size_chars=_int(raw.get("child_chunk_size_chars"), defaults.child_chunk_size_chars),
        child_chunk_overlap_chars=_int(raw.get("child_chunk_overlap_chars"), defaults.child_chunk_overlap_chars),
        max_protected_span_chars=_int(raw.get("max_protected_span_chars"), defaults.max_protected_span_chars),
        embedding_token_limit=_int(raw.get("embedding_token_limit"), defaults.embedding_token_limit),
        media_storage_provider=_text(raw.get("media_storage_provider"), defaults.media_storage_provider),
        media_storage_dir=_text(raw.get("media_storage_dir"), defaults.media_storage_dir),
        media_max_bytes=_int(raw.get("media_max_bytes"), defaults.media_max_bytes),
        preview_max_file_bytes=_int(raw.get("preview_max_file_bytes"), defaults.preview_max_file_bytes),
        preview_max_pages=_int(raw.get("preview_max_pages"), defaults.preview_max_pages),
        preview_timeout_seconds=_float(raw.get("preview_timeout_seconds"), defaults.preview_timeout_seconds),
        preview_max_chunks=_int(raw.get("preview_max_chunks"), defaults.preview_max_chunks),
        ocr_enabled=_bool(raw.get("ocr_enabled"), defaults.ocr_enabled),
        ocr_provider=_text(raw.get("ocr_provider"), defaults.ocr_provider),
        ocr_min_confidence=_float(raw.get("ocr_min_confidence"), defaults.ocr_min_confidence),
        caption_enabled=_bool(raw.get("caption_enabled"), defaults.caption_enabled),
        caption_provider=_text(raw.get("caption_provider"), defaults.caption_provider),
        dense_enabled=_bool(raw.get("dense_enabled"), defaults.dense_enabled),
        keyword_enabled=_bool(raw.get("keyword_enabled"), defaults.keyword_enabled),
        graph_enabled=_bool(raw.get("graph_enabled"), defaults.graph_enabled),
        wiki_enabled=_bool(raw.get("wiki_enabled"), defaults.wiki_enabled),
        question_generation_enabled=_bool(raw.get("question_generation_enabled"), defaults.question_generation_enabled),
    )
    inactive: list[str] = []
    warnings: list[str] = []
    parser_engine = requested.parser_engine
    if available_parser_engines and parser_engine not in available_parser_engines:
        inactive.append("parser_engine")
        warnings.append(f"parser engine '{parser_engine}' is unavailable; using builtin")
        parser_engine = "builtin"
    chunk_strategy = requested.chunk_strategy if requested.chunk_strategy in {item.value for item in ChunkStrategy} else defaults.chunk_strategy
    if chunk_strategy != requested.chunk_strategy:
        inactive.append("chunk_strategy")
        warnings.append(f"chunk strategy '{requested.chunk_strategy}' is unsupported; using {chunk_strategy}")
    ocr_enabled = requested.ocr_enabled and requested.ocr_provider not in {"", "disabled", "none"}
    if requested.ocr_enabled and not ocr_enabled:
        inactive.append("ocr_enabled")
        warnings.append("OCR is requested but no OCR provider is available")
    caption_enabled = requested.caption_enabled and caption_available and requested.caption_provider not in {"", "disabled", "none"}
    if requested.caption_enabled and not caption_enabled:
        inactive.append("caption_enabled")
        warnings.append("captioning is requested but no caption provider is available")
    effective = ProcessingEffectiveConfig(
        parser_engine=parser_engine,
        pdf_force_scanned=requested.pdf_force_scanned,
        pdf_render_dpi=_clamp(requested.pdf_render_dpi, 72, 600),
        pdf_jpeg_quality=_clamp(requested.pdf_jpeg_quality, 1, 95),
        pdf_max_pages=max(1, requested.pdf_max_pages),
        pdf_max_image_edge_px=max(256, requested.pdf_max_image_edge_px),
        pdf_render_concurrency=max(1, requested.pdf_render_concurrency),
        chunk_strategy=chunk_strategy,
        parent_child_enabled=requested.parent_child_enabled,
        parent_chunk_size_chars=max(1, requested.parent_chunk_size_chars),
        child_chunk_size_chars=max(1, requested.child_chunk_size_chars),
        child_chunk_overlap_chars=max(0, min(requested.child_chunk_overlap_chars, max(1, requested.child_chunk_size_chars) // 2)),
        max_protected_span_chars=max(1, requested.max_protected_span_chars),
        embedding_token_limit=max(0, requested.embedding_token_limit),
        media_storage_provider=requested.media_storage_provider or "local",
        media_storage_dir=str(Path(requested.media_storage_dir or defaults.media_storage_dir)),
        media_max_bytes=max(1, requested.media_max_bytes),
        preview_max_file_bytes=max(1, requested.preview_max_file_bytes),
        preview_max_pages=max(1, requested.preview_max_pages),
        preview_timeout_seconds=max(0.1, requested.preview_timeout_seconds),
        preview_max_chunks=max(1, requested.preview_max_chunks),
        ocr_enabled=ocr_enabled,
        ocr_provider=requested.ocr_provider if ocr_enabled else "disabled",
        ocr_min_confidence=max(0.0, min(1.0, requested.ocr_min_confidence)),
        caption_enabled=caption_enabled,
        caption_provider=requested.caption_provider if caption_enabled else "disabled",
        dense_enabled=requested.dense_enabled,
        keyword_enabled=requested.keyword_enabled,
        graph_enabled=requested.graph_enabled,
        wiki_enabled=requested.wiki_enabled,
        question_generation_enabled=requested.question_generation_enabled,
        inactive_overrides=tuple(dict.fromkeys(inactive)),
        warnings=tuple(warnings),
    )
    return ResolvedProcessingConfig(requested=requested, effective=effective)


def _text(value: Any, default: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or default


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _backoff_tuple(value: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = [value]
    parsed = tuple(max(0, _int(part, 0)) for part in parts if str(part).strip() != "")
    return parsed or default
