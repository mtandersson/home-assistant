"""Actions for the A Better Routeplanner integration."""

from typing import Any

from aioabrp import (
    AbrpApiError,
    AbrpAuthError,
    AbrpScopeError,
    ChargingState,
    DrivingState,
    Location,
    TelemetryPoint,
)
import probatio

from homeassistant.const import ATTR_LOCATION, ATTR_TIME, CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_get_device_and_config_entry
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import AbrpConfigEntry

SERVICE_SEND_TELEMETRY = "send_telemetry"

ATTR_VEHICLE = "vehicle"

# Not in aioabrp's Provider enum; it passes plain strings through.
TELEMETRY_PROVIDER = "HOME_ASSISTANT"

# Action field -> (TelemetryPoint field, factor from the action unit to the wire unit).
NUMERIC_FIELDS: dict[str, tuple[str, float]] = {
    "soc": ("soc_frac", 0.01),
    "soh": ("soh_frac", 0.01),
    "power": ("power_w", 1000),
    "hvac_power": ("hvac_power_w", 1000),
    "voltage": ("voltage_v", 1),
    "current": ("current_a", 1),
    "soe": ("soe_wh", 1000),
    "battery_capacity": ("battery_capacity_wh", 1000),
    "charging_energy_added": ("charging_energy_added_wh", 1000),
    "odometer": ("odometer_m", 1000),
    "elevation": ("elevation_m", 1),
    "estimated_range": ("estimated_battery_range_m", 1000),
    "speed": ("speed_ms", 1 / 3.6),
    "heading": ("heading_degrees", 1),
    "battery_temperature": ("battery_temperature_c", 1),
    "battery_temperature_min": ("battery_temperature_min_c", 1),
    "battery_temperature_max": ("battery_temperature_max_c", 1),
    "cabin_temperature": ("cabin_temperature_c", 1),
    "cabin_set_point": ("cabin_set_point_c", 1),
    "external_temperature": ("external_temperature_c", 1),
}
STATE_FIELDS: dict[str, type[ChargingState | DrivingState]] = {
    "charging_state": ChargingState,
    "driving_state": DrivingState,
}

SEND_TELEMETRY_SCHEMA = probatio.All(
    probatio.Schema(
        {
            probatio.Required(ATTR_VEHICLE): cv.string,
            probatio.Optional(ATTR_TIME): cv.datetime,
            probatio.Optional(ATTR_LOCATION): {
                probatio.Required(CONF_LATITUDE): cv.latitude,
                probatio.Required(CONF_LONGITUDE): cv.longitude,
            },
            **{
                probatio.Optional(key): probatio.Coerce(float) for key in NUMERIC_FIELDS
            },
            **{
                probatio.Optional(key): probatio.Coerce(enum)
                for key, enum in STATE_FIELDS.items()
            },
        }
    ),
    probatio.AtLeastOne(*NUMERIC_FIELDS, *STATE_FIELDS, ATTR_LOCATION),
)


def _build_point(data: dict[str, Any]) -> TelemetryPoint:
    """Convert action data to a telemetry point in wire units."""
    fields: dict[str, Any] = {
        wire: data[key] * factor
        for key, (wire, factor) in NUMERIC_FIELDS.items()
        if key in data
    }
    fields.update({key: data[key] for key in STATE_FIELDS if key in data})
    if ATTR_LOCATION in data:
        location = data[ATTR_LOCATION]
        fields["location"] = Location(
            lat=location[CONF_LATITUDE], lon=location[CONF_LONGITUDE]
        )
    return TelemetryPoint(
        provider=TELEMETRY_PROVIDER,
        time=dt_util.as_utc(data[ATTR_TIME]) if ATTR_TIME in data else dt_util.utcnow(),
        **fields,
    )


async def _async_send_telemetry(call: ServiceCall) -> None:
    """Send one telemetry point for a vehicle to A Better Routeplanner."""
    entry: AbrpConfigEntry
    device, entry = async_get_device_and_config_entry(
        call.hass, DOMAIN, call.data[ATTR_VEHICLE]
    )
    vehicle_id = next(
        (
            raw.vehicle_id
            for raw, _ in entry.runtime_data.garage_coordinator.data
            if (DOMAIN, f"{entry.unique_id}_{raw.vehicle_id}") in device.identifiers
        ),
        None,
    )
    if vehicle_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="vehicle_not_in_garage",
            translation_placeholders={
                "vehicle": device.name_by_user or device.name or device.id
            },
        )

    try:
        await entry.runtime_data.client.async_send_telemetry(
            vehicle_id, _build_point(call.data)
        )
    except AbrpAuthError as err:
        entry.async_start_reauth(call.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=(
                "telemetry_write_not_granted"
                if isinstance(err, AbrpScopeError)
                else "abrp_session_invalid"
            ),
        ) from err
    except AbrpApiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="send_telemetry_failed",
            translation_placeholders={"error": str(err)},
        ) from err


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the A Better Routeplanner actions."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_TELEMETRY,
        _async_send_telemetry,
        schema=SEND_TELEMETRY_SCHEMA,
    )
