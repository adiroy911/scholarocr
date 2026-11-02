from __future__ import annotations

import os
from pathlib import Path

import pytest

from scholarocr.config import Settings
from tests.fixtures.corpus import build_corpus

# No test may reach a real model. Developers often have GEMINI_API_KEY / OPENROUTER_API_KEY exported, and
# with SCHOLAROCR_PROVIDER=auto (the default) any of them would turn a subprocess or a stray
# Settings.from_env() into a live API call. Strip them for the whole session and pin host mode, which also
# wins over a key that load_dotenv would read from a .env in the working directory. Tests that need a
# provider pass an explicit environ / Settings. The opt-in live tests (tests/live) keep the real environment.
MODEL_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY")
if os.environ.get("SCHOLAROCR_RUN_LIVE_TESTS") != "1":
    for _name in MODEL_KEY_VARS:
        os.environ.pop(_name, None)
    os.environ["SCHOLAROCR_PROVIDER"] = "host"
    # The user-level scholarocr.env (%LOCALAPPDATA%\ScholarOCR, used by the Antigravity plugin) may hold a
    # key and a provider; point every test process and subprocess at a file that does not exist.
    os.environ["SCHOLAROCR_ENV_FILE"] = str(Path(__file__).resolve().parent / "no-user-env-file.env")


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
