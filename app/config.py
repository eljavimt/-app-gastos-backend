import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    PORT: int = Field(default=8000)
    DEBUG: bool = Field(default=True)
    
    # Base de datos
    DATABASE_URL: str = Field(default="postgresql://app_user:app_secure_password@localhost:5432/app_gastos")
    
    # Cifrado y tokens
    ENCRYPTION_KEY: str = Field(default="MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI=")
    JWT_SECRET_KEY: str = Field(default="super_secure_jwt_secret_key_app_gastos_2026")
    JWT_ALGORITHM: str = Field(default="HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60)
    
    # IMAP
    IMAP_SERVER: str = Field(default="imap.gmail.com")
    IMAP_PORT: int = Field(default=993)
    IMAP_USER: str = Field(default="dummy@gmail.com")
    IMAP_PASSWORD: str = Field(default="dummy_pass")
    
    # LLM
    LLM_PROVIDER: str = Field(default="gemini")
    GEMINI_API_KEY: str = Field(default="")
    OPENAI_API_KEY: str = Field(default="")
    ANTHROPIC_API_KEY: str = Field(default="")
    OLLAMA_BASE_URL: str = Field(default="http://localhost:11434")

    # Configuración de carga del archivo .env
    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
