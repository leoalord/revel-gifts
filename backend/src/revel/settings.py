"""Runtime configuration loaded from environment variables and optional .env files.

Precedence (highest first): process environment, ``backend/.env``, repository-root ``.env``.
Missing files are ignored, so deployed environments rely on real environment variables only.
Secret values use ``SecretStr`` and must never be logged.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_DIR = _BACKEND_DIR.parent

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Later files take priority over earlier ones.
        env_file=(_REPO_DIR / ".env", _BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_env: Literal["development", "test", "production"] = "development"
    public_base_url: str = "http://localhost:8000"
    mcp_path: str = "/mcp"
    # Extra Host header values (comma-separated), e.g. the Cloud Run *.run.app hostname.
    allowed_hosts: str = ""
    # Extra browser Origin values (comma-separated, scheme://host[:port]).
    allowed_origins: str = ""

    # --- Google Cloud ---
    gcp_project_id: str | None = None
    gcp_project_number: str | None = None
    gcp_region: str | None = None

    # --- Database ---
    # Plain str for SQLAlchemy/Alembic; repr=False keeps the embedded password out of repr/logs.
    database_url: str = Field(
        default="postgresql+psycopg://revel:revel@localhost:5432/revel", repr=False
    )

    # --- OpenFGA ---
    openfga_api_url: str = "http://localhost:8080"
    openfga_store_id: str | None = None
    openfga_authorization_model_id: str | None = None
    # Attach a Google ID token (audience = OPENFGA_API_URL) for a private Cloud Run OpenFGA.
    openfga_google_auth: bool = False

    # --- WorkOS ---
    workos_api_key: SecretStr | None = None
    workos_client_id: str | None = None
    workos_authkit_domain: str | None = None
    workos_redirect_uri: str | None = None
    workos_cookie_password: SecretStr | None = None
    # Expected access-token audience; defaults to PUBLIC_BASE_URL + MCP_PATH (mcp_resource_url).
    workos_token_audience: str | None = None

    # --- Muse Spark ---
    fireworks_api_key: SecretStr | None = None
    fireworks_model: str | None = None
    meta_api_key: SecretStr | None = None
    meta_model: str | None = None
    muse_spark_provider: Literal["fireworks", "meta"] = "fireworks"
    muse_spark_timeout_seconds: float = 8.0

    @property
    def is_development(self) -> bool:
        return self.app_env in ("development", "test")

    def validate_runtime(self) -> None:
        """Fail closed on unsafe production configuration (called by create_app)."""
        if self.app_env == "production" and urlsplit(self.public_base_url).scheme != "https":
            raise ValueError("PUBLIC_BASE_URL must use https when APP_ENV=production")

    @property
    def normalized_mcp_path(self) -> str:
        return "/" + self.mcp_path.strip("/")

    @property
    def mcp_resource_url(self) -> str:
        """MCP resource URL: WORKOS_TOKEN_AUDIENCE if set, else PUBLIC_BASE_URL + MCP_PATH."""
        if self.workos_token_audience:
            return self.workos_token_audience
        return self.public_base_url.rstrip("/") + self.normalized_mcp_path

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_base_url)
        return f"{parts.scheme}://{parts.netloc}"

    def mcp_allowed_hosts(self) -> list[str]:
        """Host header values accepted by the MCP transport (DNS-rebinding protection)."""
        hosts: list[str] = []
        netloc = urlsplit(self.public_base_url).netloc
        if netloc:
            hosts.append(netloc)
        for host in _split_csv(self.allowed_hosts):
            hosts.append(host)
        if self.is_development:
            for host in _LOCAL_HOSTS:
                hosts.extend((host, f"{host}:*"))
        return list(dict.fromkeys(hosts))

    def mcp_allowed_origins(self) -> list[str]:
        """Origin header values accepted by the MCP transport. Absent Origin is allowed."""
        origins = [self.public_origin, *_split_csv(self.allowed_origins)]
        if self.is_development:
            for host in _LOCAL_HOSTS:
                origins.extend((f"http://{host}", f"http://{host}:*"))
        return list(dict.fromkeys(origins))


@lru_cache
def get_settings() -> Settings:
    return Settings()
