"""应用配置：通过环境变量覆盖，默认值面向本地开发。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # SQLAlchemy 连接串；Compose 中注入 postgresql+psycopg://postgres:postgres@db:5432/downtime
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/downtime"


settings = Settings()
