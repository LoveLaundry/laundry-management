import os
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mongo_uri: str = Field(
        default="mongodb://localhost:27017",
        validation_alias=AliasChoices("MONGO_URI", "DATABASE_URL"),
    )
    mongo_db_name: str = "laundry_management"
    cors_origins: list[str] = ["*"]

    # Three-database architecture (mirrors bill_service)
    mongodb_main_uri: str | None = Field(default=None, validation_alias=AliasChoices("MONGODB_MAIN_URI"))
    mongodb_main_db: str | None = Field(default=None, validation_alias=AliasChoices("MONGODB_MAIN_DB"))
    mongodb_secondary_uri: str | None = Field(default=None, validation_alias=AliasChoices("MONGODB_SECONDARY_URI"))
    mongodb_secondary_db: str | None = Field(default=None, validation_alias=AliasChoices("MONGODB_SECONDARY_DB"))
    mongodb_local_uri: str | None = Field(default=None, validation_alias=AliasChoices("MONGODB_LOCAL_URI"))
    mongodb_local_db: str | None = Field(default=None, validation_alias=AliasChoices("MONGODB_LOCAL_DB"))

    def resolve_main_uri(self) -> str:
        return self.mongodb_main_uri or self.mongo_uri

    def resolve_main_db(self) -> str:
        return self.mongodb_main_db or self.mongo_db_name

    def resolve_secondary_uri(self) -> str:
        return self.mongodb_secondary_uri or "mongodb://unconfigured"

    def resolve_secondary_db(self) -> str:
        return self.mongodb_secondary_db or f"{self.resolve_main_db()}_secondary"

    def resolve_local_uri(self) -> str:
        return self.mongodb_local_uri or "mongodb://unconfigured"

    def resolve_local_db(self) -> str:
        return self.mongodb_local_db or f"{self.resolve_main_db()}_local"


settings = Settings()