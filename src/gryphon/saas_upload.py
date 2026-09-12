"""Validate uploaded specifications without fetching tenant-selected host files or references."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

import yaml

from gryphon.compiler.swagger_parser import SwaggerParser
from gryphon.errors import InputValidationError
from gryphon.models import SwaggerSource
from gryphon.runtime.execution_cleanup import finish_cleanup
from gryphon.runtime.execution_validation import json_bytes
from gryphon.saas_catalog import validate_uploaded_document

if TYPE_CHECKING:
    from gryphon.config import GryphonConfig


def _document(content: str, limit: int) -> dict[str, Any]:
    """Reject non-JSON YAML, reference fetches, cycles and oversized specification trees."""
    if len(content.encode()) > limit:
        raise InputValidationError("Specification too large")
    try:
        document = yaml.safe_load(content)
    except (yaml.YAMLError, RecursionError, ValueError):
        raise InputValidationError("Invalid specification") from None
    if not isinstance(document, dict):
        raise InputValidationError("Specification must be an object")
    json_bytes(document, limit)
    validate_uploaded_document(document, limit)
    return document


def _validate(content: str, config: GryphonConfig, limit: int) -> dict[str, Any]:
    """Compile the uploaded bytes in an owned temporary directory using the normal parser."""
    document = _document(content, limit)
    with TemporaryDirectory(prefix="gryphon-upload-") as directory:
        path = Path(directory) / "spec.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        source = SwaggerSource(name="uploaded", swagger_url=str(path), is_read_only=True)
        asyncio.run(SwaggerParser(source, max_spec_size_bytes=limit * 2, config=config).parse())
    return document


async def validate_upload(content: str, config: GryphonConfig, limit: int) -> dict[str, Any]:
    """Offload bounded parsing and await worker-owned temporary cleanup on cancellation."""
    return await finish_cleanup(asyncio.to_thread(_validate, content, config, limit))
