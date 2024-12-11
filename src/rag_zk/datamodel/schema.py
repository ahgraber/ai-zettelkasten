# %%
import datetime
import enum
import typing as t
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field as PField,
    TypeAdapter,
    computed_field,
)
from sqlmodel import Field as SMField, SQLModel

# from sqlalchemy import Boolean, Column, DateTime, Enum, Integer, String, select
# from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
# from sqlalchemy.sql import func
from typing_extensions import Annotated

# %%
# Pydantic Model
anyhttpurl_adapter = TypeAdapter(AnyHttpUrl)
ValidatedURL = Annotated[
    str,
    BeforeValidator(lambda value: str(anyhttpurl_adapter.validate_python(value))),
    AfterValidator(str),
]


class SourceLink(BaseModel):  # NOQA: D101
    model_config = ConfigDict(from_attributes=True)  # ORM mode

    url: ValidatedURL

    @computed_field
    @property
    def domain(self) -> str | None:  # NOQA:D102
        return urlparse(self.url).netloc


class ScrapeStatus(enum.Enum):  # NOQA:D101
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"
    # RETRY = "RETRY"


# %%
# class Base(DeclarativeBase):
#     pass

# class Source(Base):
#     __tablename__ = 'sources'

#     id = Column(Integer, primary_key=True, index=True)
#     url = Column(String, unique=True)
#     domain = Column(String)
#     added_at = Column(DateTime(timezone=True), server_default=func.utcnow())
#     scrape_status = Column(Enum(ScrapeStatus), default=ScrapeStatus.PENDING)
#     scraped_at = Column(DateTime(timezone=True), nullable=True)
#     content_hash = Column(String, nullable=True)
#     error_message = Column(String, nullable=True)
#     file = Column(String, nullable=True)


class Source(SQLModel, table=True):  # NOQA:D101
    uuid: UUID | None = SMField(default_factory=uuid4, primary_key=True)
    url: ValidatedURL = SMField(index=True, unique=True)
    # domain: str | None = None
    added_at: datetime.datetime = SMField(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc))
    scraped_at: datetime.datetime | None = None
    scrape_status: ScrapeStatus = ScrapeStatus("PENDING")
    content_hash: str | None = None
    error_message: str | None = None
    file: str | None = None

    @computed_field
    @property
    def domain(self) -> str | None:  # NOQA:D102
        return urlparse(self.url).netloc


# %%
