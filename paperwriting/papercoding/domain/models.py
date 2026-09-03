"""Blank paper state, serialization helpers and domain errors."""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False)


def empty_paper():
    titles = ["Abstract", "Introduction", "Methods", "Experiment", "Conclusion"]
    structure = [{"id": f"{i + 1:02}", "title": t} for i, t in enumerate(titles)]
    return dict(
        title="未命名论文",
        structure=structure,
        sections={s["id"]: "" for s in structure},
        materials={"thesis": "", "methods": {}, "notes": ""},
        metadata={},
        translations={},
        glossary="",
        outline=None,
        style={},
        template=None,
        end_matter={},
        references="",
    )


class ConflictError(ValueError):
    pass


def paragraphs(text):
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()
