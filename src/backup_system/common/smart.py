"""Normalized SMART metrics shared across the executor-to-manager boundary."""

from pydantic import BaseModel, ConfigDict, Field


class SmartMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    overall_passed: bool | None = None
    nvme_critical_warning: bool | None = None
    temperature_celsius: int | None = None
    power_on_hours: int | None = Field(default=None, ge=0)
    reallocated_sectors: int | None = Field(default=None, ge=0)
    pending_sectors: int | None = Field(default=None, ge=0)
    offline_uncorrectable: int | None = Field(default=None, ge=0)
    reported_uncorrectable: int | None = Field(default=None, ge=0)
    interface_crc_errors: int | None = Field(default=None, ge=0)
    nvme_percentage_used: int | None = Field(default=None, ge=0)
    nvme_media_errors: int | None = Field(default=None, ge=0)
