"""Constants for OEC Tariff integration."""

DOMAIN = "oec_tariff_au"
DEFAULT_API_URL = "https://api.openenergy.org.au/api/v1"
DEFAULT_SCAN_INTERVAL = 300  # seconds
DEFAULT_RETRY_AFTER = 60  # seconds, backoff on a 429 without a usable Retry-After

CONF_DNSP = "dnsp"
CONF_TARIFF = "tariff"
CONF_API_URL = "api_url"
CONF_POWER_ENTITY = "power_entity"
CONF_BILLING_DAY = "billing_day"
DEFAULT_BILLING_DAY = 1
