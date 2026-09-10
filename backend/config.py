from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # No longer used by the active extraction pipeline - OCR now runs locally
    # via Tesseract (services/tesseract_ocr_service.py). Kept as a required
    # field only because services/gemini_service.py (unused/dead code) still
    # references it; services/mistral_service.py (also unused) depends on it too.
    DATABASE_URL: str = ""
    R2_ENDPOINT_URL: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = "autus"
    R2_PREFIX: str = ""
    BOOTSTRAP_TOKEN: str = ""
    APP_ORIGIN: str = "http://localhost:3000"
    COOKIE_SECURE: bool = True
    SESSION_HOURS: int = 24
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.1-pro-preview"
    MISTRAL_API_KEY: str = ""
    # Optional explicit path to the Tesseract binary (e.g.
    # "C:\Program Files\Tesseract-OCR\tesseract.exe"). Only needed when the
    # binary is installed but NOT on the system PATH - common on Windows dev
    # machines. Leave empty to rely on PATH resolution (the default in Docker/
    # Fly.io, where apt-get installs it onto PATH already).
    TESSERACT_CMD: str = ""
    # Optional local vision-model fallback for scanned/handwritten pages that
    # Tesseract cannot read. Expected to be LM Studio/OpenAI-compatible or
    # Ollama. Example LM Studio on a Tailscale Mac:
    # http://100.79.108.26:1234
    LOCAL_VISION_OCR_BASE_URL: str = ""
    LOCAL_VISION_OCR_MODEL: str = "PaddleOCR-VL-1.6-8bit"
    LOCAL_VISION_OCR_PROVIDER: str = "auto"
    LOCAL_VISION_OCR_TIMEOUT_SECONDS: float = 180.0
    LOCAL_VISION_OCR_DPI: int = 300
    LOCAL_VISION_OCR_NUM_CTX: int = 8192
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
