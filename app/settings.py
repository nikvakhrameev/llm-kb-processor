"""Application settings loaded from environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All configuration parameters for the knowledge base system."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # --- DeepSeek (quality gate) ---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"

    # --- Anthropic / DeepSeek via claude-agent-sdk ---
    anthropic_base_url: str = "https://api.deepseek.com/v1"
    anthropic_auth_token: str = ""

    # --- Telegram ---
    telegram_bot_token: str = ""
    allowed_user_ids: list[int] = []
    owner_chat_id: int = 0

    # --- Paths ---
    kb_root: Path = Path("/Users/nikita.vakhrameev/Projects/llm-kb-wiki")
    state_db: Path = Path("/Users/nikita.vakhrameev/Projects/llm-kb/state.db")

    # --- Tuning ---
    poll_interval_seconds: int = 2
    gate_accept_threshold: int = 60
    ingest_timeout_seconds: int = 600
    ingest_max_turns: int = 25
    lint_max_turns: int = 40
    synthesis_max_turns: int = 60
    retries_max: int = 3
    retry_backoff_base_seconds: int = 60
    sweeper_stuck_minutes: int = 30

    # --- Cron ---
    lint_hour: int = 2
    lint_minute: int = 0
    synthesis_day: str = "sun"
    synthesis_hour: int = 9
    synthesis_minute: int = 0
    tz: str = "Europe/Berlin"

    # --- Git ---
    kb_git_remote: str = ""
    kb_git_autopush: bool = False


settings = Settings()
