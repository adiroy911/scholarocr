from __future__ import annotations

from pathlib import Path

import pytest

from scholarocr.config import Settings
from tests.fixtures.corpus import build_corpus


@pytest.fixture(scope="session")
def corpus(tmp_path_factory) -> dict[str, Path]:
    return build_corpus(tmp_path_factory.mktemp("corpus"))


@pytest.fixture
def settings(tmp_path, corpus) -> Settings:
    corpus_dir = next(iter(corpus.values())).parent
    return Settings(
        gemini_api_key=None,
        data_dir=tmp_path / "data",
        allowed_dirs=[corpus_dir, tmp_path],
        concurrency=2,
        gemini_rpm_limit=0,
        gemini_max_retries=2,
        render_dpi=100,
    )
