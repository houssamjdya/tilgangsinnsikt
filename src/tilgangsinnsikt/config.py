from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    # Empty values in a copied .env.example fall back to the docker-compose defaults.
    model_config = SettingsConfigDict(
        env_prefix="POSTGRES_", env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    host: str = "localhost"
    port: int = 5432
    user: str = "tilgangsinnsikt"
    password: SecretStr = SecretStr("tilgangsinnsikt")
    db: str = "tilgangsinnsikt"
