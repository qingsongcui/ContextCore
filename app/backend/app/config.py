import os
from pathlib import Path
from pydantic import BaseModel
from app.version import package_version


class Settings(BaseModel):
    app_title: str = "ContextCore"
    app_version: str = package_version()
    schema_version: str = "2026.10.24"

    @property
    def db_path(self) -> Path:
        env_db = os.getenv("CONTEXTCORE_DB")
        if env_db:
            return Path(env_db).expanduser().absolute()
        data_dir = os.getenv("CONTEXTCORE_DATA_DIR")
        if data_dir:
            return (Path(data_dir).expanduser() / "contextcore.db").absolute()
        return (Path(__file__).resolve().parent.parent / "contextcore.db").resolve()

    @property
    def upload_dir(self) -> Path:
        return self.db_path.parent / "uploads"

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:8420",
        "http://127.0.0.1:8420",
    ]


settings = Settings()
