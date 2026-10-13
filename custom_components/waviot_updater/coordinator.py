from datetime import datetime, timedelta, timezone
import logging

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify
from homeassistant.util.unit_conversion import EnergyConverter

from .const import BASE_URL, DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

INITIAL_HISTORY_DAYS = 90
UPDATE_HISTORY_DAYS = 2


class WaviotDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch WAVIoT modem and energy data safely."""

    def __init__(self, hass, api_key, modem_id):
        self.hass = hass
        self.api_key = api_key.strip()
        self.modem_id = modem_id.strip()
        self.data = {
            "battery": None,
            "temperature": None,
            "readings": [],
            "latest": None,
            "last_update": None,
        }
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

        session = async_get_clientsession(self.hass)

        # --- Modem info ---
        try:
            url = f"{BASE_URL}modem/info/?id={self.modem_id.lower()}&key={self.api_key}"
            _LOGGER.debug("Fetching modem info: %s", url)
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"HTTP {resp.status} fetching modem info from {url}")
                try:
                    info = await resp.json(content_type=None)
                except Exception as json_err:
                    body = await resp.text()
                    raise UpdateFailed(
                        f"Failed to decode modem info JSON: {json_err} (body: {body[:200]})"
                    )

            modem = info.get("modem") if isinstance(info, dict) else None
            if not modem:
                _LOGGER.warning("No modem data returned from API: %s", info)
            else:
                battery_raw = modem.get("battery")
                temperature = modem.get("temperature")
                self.data["battery"] = float(battery_raw) if battery_raw is not None else None
                self.data["temperature"] = float(temperature) if temperature is not None else None
                _LOGGER.debug(
                    "Battery: %s, Temperature: %s", self.data["battery"], self.data["temperature"]
                )

        except UpdateFailed:
            raise
        except Exception as e:
            _LOGGER.error("Exception fetching modem info: %s", e)
            raise UpdateFailed(f"Failed fetching modem info: {e}")

        # --- Energy readings ---
        try:
            channel_id = "electro_ac_p_lsum_t1"
            now = datetime.now(tz=timezone.utc)
            # Query the last 2 days on recurring updates, or the last 90 days on initial run.
            # Upstream WAVIoT API rejects from=0 with 'Reduce from/to range'.
            history_days = (
                UPDATE_HISTORY_DAYS if self._statistics_imported else INITIAL_HISTORY_DAYS
            )
            from_timestamp = int((now - timedelta(days=history_days)).timestamp())

            url = (
                f"{BASE_URL}data/get_modem_channel_values/"
                f"?modem_id={self.modem_id}&channel={channel_id}&key={self.api_key}"
                f"&from={from_timestamp}&to={int(now.timestamp())}"
            )
            _LOGGER.debug("Fetching readings from timestamp %s: %s", from_timestamp, url)

            readings = []
            async with session.get(url) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "HTTP %s fetching channel values from %s", resp.status, url
                    )
                else:
                    try:
                        ch_data = await resp.json(content_type=None)
                    except Exception as json_err:
                        body = await resp.text()
                        _LOGGER.error(
                            "Failed to decode channel values JSON: %s. Response body: %s",
                            json_err,
                            body[:500],
                        )
                        ch_data = None

                    if isinstance(ch_data, dict):
                        status = ch_data.get("status")
                        if status and status != "ok":
                            _LOGGER.warning(
                                "WAVIoT API returned status '%s': %s", status, ch_data
                            )

                        values = ch_data.get("values", {})
                        if isinstance(values, dict):
                            for ts, val in values.items():
                                try:
                                    ts_sec = int(ts)
                                    if ts_sec > 1e12:  # milliseconds → seconds
                                        ts_sec //= 1000
                                    readings.append((ts_sec, float(val)))
                                except Exception as ex:
                                    _LOGGER.warning(
                                        "Skipping invalid reading ts=%s val=%s (%s)",
                                        ts,
                                        val,
                                        ex,
                                    )
                        elif values:
                            _LOGGER.warning("Unexpected values format: %s", type(values))
                    elif ch_data is not None:
                        _LOGGER.warning("Unexpected channel data format: %s", type(ch_data))

            readings.sort(key=lambda x: x[0])
            if readings:
                self.data["readings"] = readings
            elif "readings" not in self.data:
                self.data["readings"] = []

        except Exception as e:
            _LOGGER.error("Exception fetching channel values: %s", e)
            if "readings" not in self.data:
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
            self.data.setdefault("latest", None)
            self.data.setdefault("last_update", None)
            return

        latest_timestamp, latest_value = readings[-1]
        latest_dt = datetime.fromtimestamp(latest_timestamp, tz=timezone.utc)
        self.data["latest"] = latest_value
        self.data["last_update"] = latest_dt

        _LOGGER.debug("Latest reading computed: %s at %s", latest_value, latest_dt)

    def _import_hourly_statistics(self) -> None:
        """Import readings using the provider's actual hourly intervals."""
        readings = self.data.get("readings", [])
        if not readings:
            return

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
