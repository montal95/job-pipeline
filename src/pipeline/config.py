from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Model pins — a version bump is a deliberate code change, not a config tweak.
LLM_MODEL = "claude-sonnet-4-20250514"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Anthropic
    anthropic_api_key: str = "unset"

    # Gemini (dev/test alternative — free tier via aistudio.google.com)
    gemini_api_key: str = "unset"
    llm_provider: str = "anthropic"  # "anthropic" | "gemini"
    gemini_model: str = "gemini-2.5-flash"  # override in .env if needed

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

    # Title filter — applied in merge_results before fingerprint dedup.
    # Negative keywords win over positive; empty positive list = allow all non-negative.
    title_filter_positive: str = ""
    title_filter_negative: str = (
        "manager,director,principal,staff,vp,head of,intern,qa analyst"
    )

    # Writer
    max_revision_rounds: int = 3

    # Candidate identity constants — used by fill_form for ATS text fields
    # Set these in .env; defaults are illustrative only
    candidate_first_name: str = "Samuel"
    candidate_last_name: str = "Montalvo"
    candidate_email: str = "sammontalvojr@gmail.com"
    candidate_phone: str = "214-686-7539"
    candidate_linkedin_url: str = "https://linkedin.com/in/samuel-montalvo-jr/"

    # Dice API — public key from Dice's frontend bundle; may rotate.
    # Override in .env if you have a fresher key from DevTools network inspection.
    dice_api_key: str = "1YAt0R9wBg4WfsF9VB2778F5CHLAPMVH3IWmTf45"

    # Ghost listing detection
    ghost_threshold_days: int = 14

    @property
    def sources_list(self) -> list[str]:
        return [s.strip() for s in self.enabled_sources.split(",")]

    @property
    def title_filter_positive_list(self) -> list[str]:
        return [s.strip() for s in self.title_filter_positive.split(",") if s.strip()]

    @property
    def title_filter_negative_list(self) -> list[str]:
        return [s.strip() for s in self.title_filter_negative.split(",") if s.strip()]


settings = Settings()
