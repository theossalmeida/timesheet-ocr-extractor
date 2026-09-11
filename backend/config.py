from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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
    # Paid OCR fallback, reached only for scanned pages that pdfplumber and
    # Tesseract could not read. Every call is metered and priced per token
    # (services/ai_usage.py, services/ai_pricing.py), so the model name must
    # match one litellm knows a price for.
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.8-flash"
    MISTRAL_API_KEY: str = ""
    GOOGLE_CLOUD_API_KEY: str = ""
    # Fallback USD/BRL rate used to price AI usage when the daily quote cannot
    # be fetched (services/fx.py). 0 means "no fallback": costs are then
    # recorded in USD only and reported as unknown in reais.
    USD_BRL_RATE: float = 0.0
    # Optional explicit path to the Tesseract binary (e.g.
    # "C:\Program Files\Tesseract-OCR\tesseract.exe"). Only needed when the
    # binary is installed but NOT on the system PATH - common on Windows dev
    # machines. Leave empty to rely on PATH resolution (the default in Docker/
    # Fly.io, where apt-get installs it onto PATH already).
    TESSERACT_CMD: str = ""
    # Optional free fallback for scanned/handwritten pages, tried after Gemini.
    # Expected to be LM Studio/OpenAI-compatible or Ollama. It runs on the same
    # Windows host as the services, so this is normally http://127.0.0.1:1234 -
    # note the model server must run as a service too, or it disappears when
    # the interactive session logs out and this fallback silently goes away.
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
