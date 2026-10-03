"""应用配置：通过环境变量覆盖，默认值面向本地开发。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    # SQLAlchemy 连接串；Compose 中注入 postgresql+psycopg://postgres:postgres@db:5432/downtime
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/downtime"

    # 班次快照续页游标（HMAC-SHA256）签名密钥：同一部署的全部进程需保持一致，
    # 生产部署应显式注入随机值；游标不透明，签名仅用于完整性与防伪，载荷本身不加密。
    cursor_secret: str = "downtime-snapshot-cursor-dev-secret"


settings = Settings()
