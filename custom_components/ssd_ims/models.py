"""Data models for SSD IMS integration."""

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator


def _extract_pod_id(text: str) -> str:
    """Extract the stable 16-20 character POD ID from free-text POD data.

    Raises:
        ValueError: If a valid POD ID cannot be extracted from ``text``.
    """
    # Format is typically "99XXX1234560000G (Rodinný dom)" — look for a
    # 16-20 character alphanumeric ID at the start.
    if match := re.match(r"^([A-Z0-9]{16,20})", text):
        return match.group(1)

    # Or the text may already be a bare POD ID with nothing else appended.
    if re.match(r"^[A-Z0-9]{16,20}$", text):
        return text

    raise ValueError(
        f"Could not extract valid POD ID from text: {text} (length: {len(text)})"
    )


class UserProfile(BaseModel):
    """User profile model."""

    user_id: int = Field(alias="userId")
    username: str
    full_name: str = Field(alias="fullName")
    email: str
    created_on: datetime = Field(alias="createdOn")
    changed_on: datetime = Field(alias="changedOn")


class AuthResponse(BaseModel):
    """Authentication response model."""

    user_profile: UserProfile = Field(alias="userProfile")
    user_actions: list[int] = Field(alias="userActions")
    password_expiration_date: datetime = Field(alias="passwordExpirationDate")
    show_password_change_warning: bool = Field(alias="showPasswordChangeWarning")


class PointOfDelivery(BaseModel):
    """Point of delivery model.

    ``id`` — the stable POD ID — is extracted from ``text`` once, at
    construction time, rather than lazily on every access: a POD with
    unparseable text now fails to construct at all (raising ValidationError,
    a ValueError subclass) instead of silently existing as a half-valid
    object whose `.id` can blow up wherever it happens to be read. Callers
    that construct PointOfDelivery from API data (see
    SsdImsApiClient.get_points_of_delivery) handle that single point of
    failure by skipping the offending entry, so every PointOfDelivery
    instance that exists elsewhere in the codebase is guaranteed to have a
    valid, side-effect-free `.id`.
    """

    text: str
    value: str
    id: str = ""

    @model_validator(mode="after")
    def _populate_id(self) -> "PointOfDelivery":
        self.id = _extract_pod_id(self.text)
        return self


class ChartData(BaseModel):
    """Summary chart data model."""

    metering_datetime: list[str] = Field(alias="meteringDatetime", default_factory=list)
    actual_consumption: list[float] = Field(
        alias="actualConsumption", default_factory=list
    )
    actual_supply: list[float] = Field(alias="actualSupply", default_factory=list)
    idle_consumption: list[float] = Field(alias="idleConsumption", default_factory=list)
    idle_supply: list[float] = Field(alias="idleSupply", default_factory=list)
    sum_actual_consumption: float | None = Field(
        alias="sumActualConsumption", default=0.0
    )
    sum_actual_supply: float | None = Field(alias="sumActualSupply", default=0.0)
    sum_idle_consumption: float | None = Field(alias="sumIdleConsumption", default=0.0)
    sum_idle_supply: float | None = Field(alias="sumIdleSupply", default=0.0)

    @field_validator(
        "actual_consumption",
        "actual_supply",
        "idle_consumption",
        "idle_supply",
        mode="before",
    )
    @classmethod
    def validate_float_lists(cls, v: Any, info: ValidationInfo) -> list[float]:
        """Validate float lists with enhanced error messages."""
        if not isinstance(v, list):
            # Handle single value case
            if v is None:
                return []
            try:
                return [float(v)]
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Field '{info.field_name}': Expected list or numeric value, got {type(v).__name__}: {v}"
                ) from exc

        # Process list values
        result = []
        for i, item in enumerate(v):
            if item is None:
                # None is valid for supply data when no generation occurs, but
                # the position must be preserved: coordinator.py indexes this
                # list positionally against metering_datetime, so dropping the
                # entry instead of zero-filling it would misalign every value
                # that follows.
                result.append(0.0)
                continue
            try:
                result.append(float(item))
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"Field '{info.field_name}' at index {i}: "
                    f"Cannot convert '{item}' (type: {type(item).__name__}) to float. "
                    f"Raw data at position {i}: {repr(item)}. "
                    f"Context around position {i}: {v[max(0, i - 2) : i + 3]}. "
                    f"Original error: {str(e)}"
                ) from e

        return result

    @field_validator(
        "sum_actual_consumption",
        "sum_actual_supply",
        "sum_idle_consumption",
        "sum_idle_supply",
        mode="before",
    )
    @classmethod
    def validate_sum_fields(cls, v: Any, info: ValidationInfo) -> float:
        """Validate sum fields with enhanced error messages."""
        if v is None:
            return 0.0

        try:
            return float(v)
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"Field '{info.field_name}': Cannot convert '{v}' (type: {type(v).__name__}) to float. "
                f"Raw value: {repr(v)}. Original error: {str(e)}"
            ) from e


# Columns requested from the registry-data grid endpoint, in order. The
# portal only returns values for the columns listed in the request, as a
# positional ``values`` array per row.
REGISTRY_COLUMNS_BASE: tuple[str, ...] = (
    "serialNumber",
    "billingPeriodEndDatetime",
    "actualConsumptionMax",
    "actualConsumptionTotal",
    "actualConsumptionTariff1",
    "actualConsumptionTariff2",
    "actualSupplyTotal",
)
# Prefer requesting min after max; fall back to BASE if the portal rejects it.
REGISTRY_COLUMNS: tuple[str, ...] = (
    "serialNumber",
    "billingPeriodEndDatetime",
    "actualConsumptionMax",
    "actualConsumptionMin",
    "actualConsumptionTotal",
    "actualConsumptionTariff1",
    "actualConsumptionTariff2",
    "actualSupplyTotal",
)


class RegistryReading(BaseModel):
    """One meter register reading (Registrové údaje).

    Energy values are cumulative meter counters (OBIS 1.8.x / 2.8.0), not
    per-period totals; ``max_demand`` (1.6.0) is the peak quarter-hour
    demand of the period ending at ``reading_time``. ``min_demand`` is the
    matching low when the portal returns ``actualConsumptionMin``.
    """

    serial_number: str
    reading_time: datetime
    max_demand: float | None = None
    min_demand: float | None = None
    consumption_total: float | None = None
    consumption_tariff1: float | None = None
    consumption_tariff2: float | None = None
    supply_total: float | None = None

    @classmethod
    def from_row(
        cls,
        values: list[Any],
        columns: tuple[str, ...] | None = None,
    ) -> "RegistryReading":
        """Build a reading from a positional row aligned with the columns used."""
        if columns is None:
            if len(values) >= len(REGISTRY_COLUMNS):
                columns = REGISTRY_COLUMNS
            elif len(values) >= len(REGISTRY_COLUMNS_BASE):
                columns = REGISTRY_COLUMNS_BASE
            else:
                raise ValueError(
                    f"Registry row has {len(values)} values, "
                    f"expected at least {len(REGISTRY_COLUMNS_BASE)}: {values!r}"
                )
        elif len(values) < len(columns):
            raise ValueError(
                f"Registry row has {len(values)} values, "
                f"expected {len(columns)}: {values!r}"
            )
        row = dict(zip(columns, values, strict=False))
        return cls(
            serial_number=str(row["serialNumber"] or ""),
            reading_time=row["billingPeriodEndDatetime"],
            max_demand=row["actualConsumptionMax"],
            min_demand=row.get("actualConsumptionMin"),
            consumption_total=row["actualConsumptionTotal"],
            consumption_tariff1=row["actualConsumptionTariff1"],
            consumption_tariff2=row["actualConsumptionTariff2"],
            supply_total=row["actualSupplyTotal"],
        )

    @field_validator("reading_time", mode="before")
    @classmethod
    def _parse_reading_time(cls, v: Any) -> Any:
        """Parse the portal's 7-digit-fraction UTC timestamps."""
        if isinstance(v, str):
            # e.g. "2026-10-31T23:00:00.0000000Z" — Python's fromisoformat
            # accepts at most 6 fractional digits.
            text = v.replace("Z", "+00:00")
            if "." in text:
                head, _, tail = text.partition(".")
                # Only fractional digits; do not swallow +HH:MM offset digits.
                match = re.match(r"^(\d+)(.*)$", tail)
                if match:
                    digits, offset = match.group(1), match.group(2)
                    text = f"{head}.{digits[:6].ljust(6, '0')}{offset}"
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        return v
