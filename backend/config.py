from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # No longer used by the active extraction pipeline — OCR now runs locally
    # via Tesseract (services/tesseract_ocr_service.py). Kept as a required
    # field only because services/gemini_service.py (unused/dead code) still
    # references it; services/mistral_service.py (also unused) depends on it too.
    GEMINI_API_KEY: str
    MISTRAL_API_KEY: str = ""
    # Optional explicit path to the Tesseract binary (e.g.
    # "C:\Program Files\Tesseract-OCR\tesseract.exe"). Only needed when the
    # binary is installed but NOT on the system PATH — common on Windows dev
    # machines. Leave empty to rely on PATH resolution (the default in Docker/
    # Fly.io, where apt-get installs it onto PATH already).
    TESSERACT_CMD: str = ""
    ENVIRONMENT: str = "development"
    MAX_FILE_SIZE_MB: int = 50
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]
    LOG_LEVEL: str = "INFO"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: object) -> object:
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
