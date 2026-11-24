"""Settings loaded from the environment and `.env` (see `.env.example`)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://analyst:analyst@localhost:5433/analyst"
    # SEC fair access: a real name + contact email. Required for any live EDGAR request.
    sec_user_agent: str = ""
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    data_dir: Path = Path("data")
    # Part 2: cross-encoder rerank of fused search results (slower; needs the ml extra).
    rerank: bool = False
    # Part 3: agent. Effort for the tool loop (low | medium | high | xhigh | max).
    agent_effort: str = "medium"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def universe_csv(self) -> Path:
        return self.data_dir / "universe.csv"

    @property
    def prices_csv(self) -> Path:
        return self.data_dir / "prices.csv"


@lru_cache
def get_settings() -> Settings:
    return Settings()
