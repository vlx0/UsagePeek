"""User-added local AI sources: %USERPROFILE%\\.usagepeek\\sources.json"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class CustomSource:
    id: str
    label: str
    path: str

    @staticmethod
    def create(label: str, path: str) -> "CustomSource":
        return CustomSource(id=f"custom-{uuid.uuid4().hex[:8]}", label=label, path=path)


def config_path() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / ".usagepeek" / "sources.json"


def load() -> list[CustomSource]:
    path = config_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[CustomSource] = []
    for item in raw.get("sources", []):
        if isinstance(item, dict) and item.get("path"):
            out.append(
                CustomSource(
                    id=str(item.get("id") or uuid.uuid4().hex[:8]),
                    label=str(item.get("label") or Path(item["path"]).name),
                    path=str(item["path"]),
                )
            )
    return out


def save(sources: list[CustomSource]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sources": [asdict(s) for s in sources]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def add(label: str, path: str) -> list[CustomSource]:
    sources = load()
    sources.append(CustomSource.create(label, path))
    save(sources)
    return sources


def remove(source_id: str) -> list[CustomSource]:
    sources = [s for s in load() if s.id != source_id]
    save(sources)
    return sources
