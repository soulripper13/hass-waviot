from datetime import datetime, timedelta, timezone
import logging

import aiohttp

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import EnergyConverter

from .const import BASE_URL, DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

class WaviotDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch WAVIoT modem and energy data safely."""

    def __init__(self, hass, api_key, modem_id):
        self.hass = hass
        self.api_key = api_key
        self.modem_id = modem_id
        self.data = {}
        self._statistics_imported = False
        super().__init__(
            hass,
            _LOGGER,
            name=f"WAVIoT Modem {modem_id}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self):
        """Fetch modem info and channel readings safely."""
        if self.data is None:
            self.data = {}

        async with aiohttp.ClientSession() as session:
            # --- Modem info ---
            try:
                url = f"{BASE_URL}modem/info/?id={self.modem_id.lower()}&key={self.api_key}"
                _LOGGER.debug("Fetching modem info: %s", url)
                async with session.get(url) as resp:
                    info = await resp.json()
                modem = info.get("modem") if info else None
                if not modem:
                    _LOGGER.warning("No modem data returned from API")
                    self._init_empty_data()
                    return self.data

                battery_raw = modem.get("battery")
                temperature = modem.get("temperature")
                self.data["battery"] = float(battery_raw) if battery_raw is not None else None
                self.data["temperature"] = temperature
                _LOGGER.debug("Battery: %s, Temperature: %s", self.data["battery"], temperature)

            except Exception as e:
                _LOGGER.error("Exception fetching modem info: %s", e)
                raise UpdateFailed(f"Failed fetching modem info: {e}")

            # --- Energy readings ---
            try:
                channel_id = "electro_ac_p_lsum_t1"
                now = datetime.now(tz=timezone.utc)
                from_timestamp = (
                    int((now - timedelta(days=2)).timestamp())
                    if self._statistics_imported
                    else 0
                )

                url = (
                    f"{BASE_URL}data/get_modem_channel_values/"
                    f"?modem_id={self.modem_id}&channel={channel_id}&key={self.api_key}"
                    f"&from={from_timestamp}&to={int(now.timestamp())}"
                )
                _LOGGER.debug("Fetching readings from timestamp %s", from_timestamp)

                readings = []
                async with session.get(url) as resp:
                    ch_data = await resp.json()
                    values = ch_data.get("values", {}) if ch_data else {}
                    for ts, val in values.items():
                        try:
                            ts_sec = int(ts)
                            if ts_sec > 1e12:  # milliseconds → seconds
                                ts_sec //= 1000
                            readings.append((ts_sec, float(val)))
                        except Exception as ex:
                            _LOGGER.warning("Skipping invalid reading ts=%s val=%s (%s)", ts, val, ex)

                readings.sort(key=lambda x: x[0])
                self.data["readings"] = readings

            except Exception as e:
                _LOGGER.error("Exception fetching channel values: %s", e)
                self.data["readings"] = []

            # --- Compute latest metric only ---
            self._compute_latest()
            self._import_hourly_statistics()

        return self.data

    def _compute_latest(self):
        """Compute only the latest reading value."""
        if self.data is None:
            self.data = {}

        readings = self.data.get("readings", [])
        if not readings:
            _LOGGER.debug("No readings available to compute latest value.")
            self._init_empty_data()
            return

        latest_timestamp, latest_value = readings[-1]
        latest_dt = datetime.fromtimestamp(latest_timestamp, tz=timezone.utc)
        self.data["latest"] = latest_value
        self.data["last_update"] = latest_dt

        _LOGGER.debug("Latest reading computed: %s at %s", latest_value, latest_dt)

    def _import_hourly_statistics(self) -> None:
        """Import readings using the provider's actual hourly intervals."""
        readings = self.data.get("readings", [])
        if self._statistics_imported:
            # Re-import two days so late provider corrections replace existing rows.
            readings = readings[-49:]
        statistics = []

        for previous, current in zip(readings, readings[1:], strict=False):
            previous_timestamp, previous_value = previous
            current_timestamp, current_value = current

            # WAVIoT timestamps a reading at the end of the measured hour.
            if current_timestamp - previous_timestamp != 3600:
                continue

            consumption = current_value - previous_value
            if consumption < 0:
                _LOGGER.warning(
                    "Skipping hourly statistic at %s because the meter total reset",
                    current_timestamp,
                )
                continue

            statistics.append(
                StatisticData(
                    start=datetime.fromtimestamp(previous_timestamp, tz=timezone.utc),
                    state=consumption,
                    sum=current_value,
                )
            )

        if not statistics:
            return

        statistic_id = f"{DOMAIN}:{slugify(self.modem_id)}_energy_consumption"
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"WAVIoT {self.modem_id} Energy Consumption",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        async_add_external_statistics(self.hass, metadata, statistics)
        self._statistics_imported = True
        _LOGGER.debug(
            "Imported %s hourly statistics for %s", len(statistics), statistic_id
        )

    def _init_empty_data(self):
        """Initialize empty data dict."""
        self.data.update({
            "battery": None,
            "temperature": None,
            "readings": [],
            "latest": None,
            "last_update": None,
        })
