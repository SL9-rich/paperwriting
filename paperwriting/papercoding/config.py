"""Project-local configuration; importing this module never loads user data."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_config():
    from dotenv import load_dotenv

    # Do not search parent folders or override explicitly set environment values.
    load_dotenv(ROOT / ".env", override=False)


def data_directory():
    return Path(os.getenv("PAPERCODING_DATA_DIR", str(ROOT / "data"))).expanduser()


def embeddings_enabled():
    return os.getenv("PAPERCODING_ENABLE_EMBEDDINGS", "false").lower() in {"1", "true", "yes"}


def embedding_cache():
    return Path(
        os.getenv("PAPERCODING_EMBEDDING_CACHE", str(ROOT / ".cache/embeddings"))
    ).expanduser()
