from pathlib import Path

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


RULES_DIR = Path(__file__).resolve().parents[2] / "rules"


class EntraSettings(BaseSettings):
    # Fields stay optional so the rest of the tool runs without Entra; the connector
    # checks for the required ones before it makes any request.
    model_config = SettingsConfigDict(
        env_prefix="ENTRA_", env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    identity_map: Path | None = None
    privileged_groups: Path = RULES_DIR / "entra_privileged_groups.yaml"


class SodGroupSettings(BaseSettings):
    # Real group names belong in .env, never in a tracked file. A separate class keeps the
    # SoD analysis from loading the Entra credentials at all.
    model_config = SettingsConfigDict(
        env_prefix="ENTRA_", env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    sod_groups: dict[str, str] = {"entra:Utviklere": "Utviklere", "entra:GA-admins": "GA-admins"}
