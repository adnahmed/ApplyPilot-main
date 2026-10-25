"""Immutable source records for observations, exact excerpts, and learning evidence.

The previous immutable source format is retained for resumed applications and
verbatim final-result references. This module does not schedule model requests
or implement MCP tools.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid


class SourceFiles:
    """Persist application evidence with content references and provenance."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, ref: str, suffix: str) -> Path:
        if not isinstance(ref, str) or re.fullmatch(r"[a-f0-9]{64}", ref) is None:
            raise ValueError("Use a source ref from the supplied source catalog")
        path = self.root / (ref + suffix)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("Source is outside the owning context")
        return path

    @staticmethod
    def _write(path: Path, text: str) -> None:
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def put(self, label: str, text: str, *, kind: str = "text", sources: list | None = None) -> dict:
        ref = hashlib.sha256((kind + "\0" + label + "\0" + text).encode("utf-8")).hexdigest()
        meta = self._path(ref, ".meta.json")
        if meta.exists():
            return self.info(ref)
        self._write(self._path(ref, ".txt"), text)
        record = {"ref": ref, "label": label, "kind": kind, "chars": len(text),
                  "recorded_at_ns": time.time_ns(), "sources": sources or []}
        self._write(meta, json.dumps(record, ensure_ascii=False))
        return {**record, "path": str(self._path(ref, ".txt"))}

    def info(self, ref: str) -> dict:
        record = json.loads(self._path(ref, ".meta.json").read_text(encoding="utf-8"))
        return {**record, "path": str(self._path(ref, ".txt"))}

    def read(self, ref: str, start: int = 0, end: int | None = None) -> dict:
        record = self.info(ref)
        if type(start) is not int or start < 0 or (end is not None and (type(end) is not int or end < start)):
            raise ValueError("Use nonnegative character offsets with end >= start")
        text = self._path(ref, ".txt").read_text(encoding="utf-8")
        if start > len(text) or (end is not None and end > len(text)):
            raise ValueError(f"Selection is outside source length {len(text)}")
        stop = len(text) if end is None else end
        return {**record, "start": start, "end": stop, "text": text[start:stop],
                "next_start": stop if stop < len(text) else None}

    def seed(self, context: dict, label: str = "input") -> list[dict]:
        records = []
        for key, value in context.items():
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
            original = self.put(f"{label}.{key}", text)
            records.append(original)
            if key == "html" and isinstance(value, str):
                # HTML is a declared source type. Parsing is ordinary host code,
                # not an LLM writing a parser or guessing an application URL.
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(value, "html.parser")
                for element in soup(["script", "style"]):
                    element.decompose()
                source = [{"ref": original["ref"], "start": 0, "end": len(value)}]
                records.append(self.put(f"{label}.html.text", soup.get_text("\n", strip=True), sources=source))
                links = [{"text": a.get_text(" ", strip=True), "href": a["href"]}
                         for a in soup.find_all("a", href=True)]
                records.append(self.put(f"{label}.html.links", json.dumps(links, ensure_ascii=False, indent=2), sources=source))
        return records

    def capture(self, value, label: str) -> list[dict]:
        """Export tool/event data; preserve image blocks separately, never as OCR text."""
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json", by_alias=True)
        if isinstance(value, (list, tuple)):
            value = [v.model_dump(mode="json", by_alias=True) if hasattr(v, "model_dump") else v for v in value]
        records = []

        def walk(item, path):
            if isinstance(item, dict):
                kind = item.get("type")
                if kind in {"image", "image_url", "input_image"}:
                    records.append(self.put(path, json.dumps(item, ensure_ascii=False), kind="image"))
                    return {"image_ref": records[-1]["ref"],
                            "note": "Image payload archived separately; inspect the original native image, not its base64 as text."}
                # Preserve protocol structure while keeping image bytes out of text search.
                return {k: walk(v, path + "." + k) for k, v in item.items()}
            if isinstance(item, list):
                return [walk(v, f"{path}[{i}]") for i, v in enumerate(item)]
            return item

        text_value = walk(value, label)
        records.insert(0, self.put(label, json.dumps(text_value, ensure_ascii=False, indent=2, default=str)))
        return records

    def expand(self, value):
        """Let a root return long verbatim text by reference instead of re-generating it."""
        if isinstance(value, str):
            match = re.fullmatch(r"context://([a-f0-9]{64})/(\d+)/(\d+)", value)
            if match:
                ref, start, end = match.groups()
                result = self.read(ref, int(start), int(end))
                if result["kind"] == "image":
                    raise ValueError("An image reference is not a text answer")
                return result["text"]
        if isinstance(value, list):
            return [self.expand(v) for v in value]
        if isinstance(value, dict):
            return {k: self.expand(v) for k, v in value.items()}
        return value
