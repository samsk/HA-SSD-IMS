"""Data models for SSD IMS integration."""
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, ValidationInfo


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
    user_actions: List[int] = Field(alias="userActions")
    password_expiration_date: datetime = Field(alias="passwordExpirationDate")
    show_password_change_warning: bool = Field(alias="showPasswordChangeWarning")


class PointOfDelivery(BaseModel):
    """Point of delivery model."""

    text: str
    value: str

    @property
    def id(self) -> str:
        """Extract stable 16-20 character POD ID from text.

        Returns:
            Extracted POD ID (16-20 chars)

        Raises:
            ValueError: If a valid POD ID cannot be extracted
        """
        # First, try to extract POD number from format like
        # "99XXX1234560000G (Rodinný dom)"
        # Look for 16-20 character alphanumeric strings at the start
        match = re.search(r"^([A-Z0-9]{16,20})", self.text)
        if match:
            extracted_id = match.group(1)
            # Verify it's exactly 16-20 characters
            if 16 <= len(extracted_id) <= 20:
                return extracted_id
            else:
                raise ValueError(
                    f"Extracted ID length invalid: {extracted_id} "
                    f"(length: {len(extracted_id)}, expected 16-20)"
                )

        # If that fails, check if it's already a POD number format (16-20 chars)
        if re.match(r"^[A-Z0-9]{16,20}$", self.text):
            return self.text

        # If we get here, we couldn't extract a valid POD ID
        raise ValueError(
            f"Could not extract valid POD ID from text: {self.text} "
            f"(length: {len(self.text)})"
        )


class PodNameMapping(BaseModel):
    """POD name mapping model for friendly names."""

    pod_id: str
    original_name: str
    friendly_name: str


class QualityType(BaseModel):
    """Quality type model."""

    value: str
    text: str
    codebook: str


class MeteringDataRow(BaseModel):
    """Individual metering data row."""

    values: List[Any]


class MeteringDataResponse(BaseModel):
    """Metering data response model."""

    columns: List[Dict[str, Any]]
    rows: List[MeteringDataRow]
    page: Optional[Dict[str, Any]] = None


class MeteringData(BaseModel):
    """Processed metering data point."""

    metering_datetime: datetime
    period: int
    actual_consumption: Optional[float] = None
    actual_supply: Optional[float] = None
    idle_consumption: Optional[float] = None
    idle_supply: Optional[float] = None


class ChartData(BaseModel):
    """Summary chart data model."""

    metering_datetime: List[str] = Field(alias="meteringDatetime", default_factory=list)
    actual_consumption: List[float] = Field(
        alias="actualConsumption", default_factory=list
    )
    actual_supply: List[float] = Field(alias="actualSupply", default_factory=list)
    idle_consumption: List[float] = Field(alias="idleConsumption", default_factory=list)
    idle_supply: List[float] = Field(alias="idleSupply", default_factory=list)
    sum_actual_consumption: Optional[float] = Field(
        alias="sumActualConsumption", default=0.0
    )
    sum_actual_supply: Optional[float] = Field(alias="sumActualSupply", default=0.0)
    sum_idle_consumption: Optional[float] = Field(
        alias="sumIdleConsumption", default=0.0
    )
    sum_idle_supply: Optional[float] = Field(alias="sumIdleSupply", default=0.0)

    @field_validator('actual_consumption', 'actual_supply', 'idle_consumption', 'idle_supply', mode='before')
    @classmethod
    def validate_float_lists(cls, v: Any, info: ValidationInfo) -> List[float]:
        """Validate float lists with enhanced error messages."""
        if not isinstance(v, list):
            # Handle single value case
            if v is None:
                return []
            try:
                return [float(v)]
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Field '{info.field_name}': Expected list or numeric value, got {type(v).__name__}: {v}") from exc
        
        # Process list values
        result = []
        for i, item in enumerate(v):
            if item is None:
                # Skip None values - they're valid for supply data when no generation occurs
                continue
            try:
                result.append(float(item))
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"Field '{info.field_name}' at index {i}: "
                    f"Cannot convert '{item}' (type: {type(item).__name__}) to float. "
                    f"Raw data at position {i}: {repr(item)}. "
                    f"Context around position {i}: {v[max(0, i-2):i+3]}. "
                    f"Original error: {str(e)}"
                ) from e
        
        return result

    @field_validator('sum_actual_consumption', 'sum_actual_supply', 'sum_idle_consumption', 'sum_idle_supply', mode='before')
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


class AggregatedData(BaseModel):
    """Aggregated data for different time periods.
    
    Note: This model is flexible and can contain any time period keys
    as defined in TIME_PERIODS_CONFIG. The structure is:
    {
        "period_key": {
            "sensor_type": float_value,
            ...
        },
        ...
    }
    """
    
    # Allow arbitrary fields for dynamic time periods
    class Config:
        extra = "allow"
