from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Model pin — a version bump is a deliberate code change, not a config tweak.
LLM_MODEL = "claude-sonnet-4-20250514"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Anthropic
    anthropic_api_key: str = "unset"

    # Paths
    cv_path: str = "./cv.pdf"
    app_db_path: str = "./data/pipeline.db"
    checkpoint_db_path: str = "./data/checkpoints.db"
    output_dir: str = "./output"

    # Search defaults
    default_location: str = "Chicago, IL"
    default_remote: bool = True
    default_max_results_per_source: int = 25
    enabled_sources: str = "indeed,dice,linkedin,ziprecruiter"

    # Writer
    max_revision_rounds: int = 3

    # Ghost listing detection
    ghost_threshold_days: int = 14

    @property
    def sources_list(self) -> list[str]:
        return [s.strip() for s in self.enabled_sources.split(",")]


settings = Settings()
