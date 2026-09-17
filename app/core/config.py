from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_env: str = "development"
    database_url: SecretStr = SecretStr("postgresql+psycopg://billing:billing-local-only@postgres/billing")
    openstack_cloud_id: UUID = UUID("00000000-0000-0000-0000-000000000001")
    openstack_cloud_name: str = "OpenStack-Test"
    os_auth_url: str = Field(default="", repr=False)
    os_username: str = Field(default="", repr=False)
    os_password: SecretStr = SecretStr("")
    os_project_name: str = Field(default="", repr=False)
    os_user_domain_name: str = "Default"
    os_project_domain_name: str = "Default"
    os_region_name: str = "RegionOne"
    os_interface: str = "internal"
    os_application_credential_id: str = Field(default="", repr=False)
    os_application_credential_secret: SecretStr = SecretStr("")
    os_cacert: str = ""
    os_compute_api_version: str = "2.47"
    os_api_timeout_seconds: int = Field(default=30, ge=1, le=300)
    sync_interval_seconds: int = Field(default=300, ge=5)
    sync_enabled: bool = True
    missing_scan_threshold: int = Field(
        default=3,
        ge=2,
        validation_alias=AliasChoices(
            "RESOURCE_MISSING_CONFIRMATION_COUNT", "MISSING_SCAN_THRESHOLD", "missing_scan_threshold"
        ),
    )
    billing_policy_path: str = "config/billing.yaml"
    retain_raw_payload: bool = False
    billing_currency: Literal["VND"] = "VND"
    price_cpu_per_vcpu_hour: Decimal = Field(default=Decimal("10000"), ge=0, max_digits=24, decimal_places=8)
    price_ram_per_gib_hour: Decimal = Field(default=Decimal("11000"), ge=0, max_digits=24, decimal_places=8)
    price_ssd_per_gib_hour: Decimal = Field(default=Decimal("500"), ge=0, max_digits=24, decimal_places=8)
    billing_timezone: str = "Asia/Ho_Chi_Minh"
    metering_policy_path: str = "config/metering.yaml"
    metering_calculation_version: str = Field(default="meter-v1", pattern=r"^meter-v[0-9]+$", max_length=40)
    metering_enabled: bool = True
    rating_enabled: bool = True
    rating_calculation_version: str = Field(default="rating-v1", pattern=r"^rating-v[0-9]+$", max_length=40)
    billing_admin_token: SecretStr = SecretStr("")
    billing_operator_token: SecretStr = SecretStr("")
    allow_provisional_in_draft: bool = True
    allow_provisional_in_final: bool = False
    pricing_admin_token: SecretStr = SecretStr("")

    @field_validator("billing_timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("BILLING_TIMEZONE must be a valid IANA timezone") from None
        return value
