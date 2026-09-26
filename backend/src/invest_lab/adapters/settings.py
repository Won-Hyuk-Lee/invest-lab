"""Explicit paper-only configuration. Loading configuration never contacts a broker."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv.parser import parse_stream

PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
PAPER_WS_URL = "ws://ops.koreainvestment.com:31000/tryitout"


class SettingsError(ValueError):
    """Messages identify fields, never values from a credentials file."""


@dataclass(frozen=True)
class KisSettings:
    app_key: str = field(default="", repr=False)
    app_secret: str = field(default="", repr=False)
    account_number: str = field(default="", repr=False)
    account_product_code: str = field(default="", repr=False)
    hts_id: str = field(default="", repr=False)
    base_url: str = PAPER_BASE_URL
    ws_url: str = PAPER_WS_URL
    cache_path: Path = field(default=Path(".local/kis-coordination.db"), repr=False)
    request_interval: float = 1.25
    timeout: float = 15.0
    orders_enabled: bool = False

    def __post_init__(self):
        if self.base_url != PAPER_BASE_URL or self.ws_url != PAPER_WS_URL:
            raise SettingsError("Only the approved KIS paper endpoints are supported.")
        if not 1.0 <= self.request_interval <= 60:
            raise SettingsError("KIS_REQUEST_INTERVAL must be between 1 and 60 seconds.")
        if not 1 <= self.timeout <= 120:
            raise SettingsError("KIS_TIMEOUT must be between 1 and 120 seconds.")
        if self.account_number and not re.fullmatch(r"[0-9]{8}", self.account_number):
            raise SettingsError("KIS_PAPER_ACCOUNT_NUMBER must contain 8 digits.")
        if self.account_product_code and not re.fullmatch(r"[0-9]{2}", self.account_product_code):
            raise SettingsError("KIS_PAPER_ACCOUNT_PRODUCT_CODE must contain 2 digits.")
        if any(
            "\n" in value or "\r" in value for value in (self.app_key, self.app_secret, self.hts_id)
        ):
            raise SettingsError("Credential fields cannot contain line breaks.")
        object.__setattr__(self, "cache_path", Path(self.cache_path))

    @classmethod
    def from_mapping(cls, values: Mapping[str, str | None], *, root: Path | None = None):
        def value(key: str, default: str = "") -> str:
            return str(values.get(key) or default).strip()

        try:
            interval = float(value("KIS_REQUEST_INTERVAL", "1.25"))
            timeout = float(value("KIS_TIMEOUT", "15"))
        except ValueError:
            raise SettingsError("KIS timeout/interval must be numeric.") from None
        enabled = value("KIS_PAPER_ORDERS_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise SettingsError("KIS_PAPER_ORDERS_ENABLED must be true or false.")
        path = Path(value("KIS_CACHE_PATH", ".local/kis-coordination.db"))
        if root is not None and not path.is_absolute():
            path = root / path
        return cls(
            app_key=value("KIS_PAPER_APP_KEY"),
            app_secret=value("KIS_PAPER_APP_SECRET"),
            account_number=value("KIS_PAPER_ACCOUNT_NUMBER"),
            account_product_code=value("KIS_PAPER_ACCOUNT_PRODUCT_CODE"),
            hts_id=value("KIS_PAPER_HTS_ID"),
            base_url=value("KIS_PAPER_BASE_URL", PAPER_BASE_URL),
            ws_url=value("KIS_PAPER_WS_URL", PAPER_WS_URL),
            cache_path=path,
            request_interval=interval,
            timeout=timeout,
            orders_enabled=enabled == "true",
        )

    @classmethod
    def from_env_file(cls, path: Path, *, environ: Mapping[str, str] | None = None):
        """Standard dotenv only; no interpolation and no mutation of process environment."""
        path = Path(path)
        values = {}
        if path.exists():
            # The normal parser supports export, quotes, comments and literal '$'.
            # Its higher-level loader logs malformed lines, which might be secrets.
            try:
                with path.open(encoding="utf-8-sig") as stream:
                    for binding in parse_stream(stream):
                        if binding.error:
                            raise SettingsError("Invalid dotenv syntax.")
                        if binding.key is not None:
                            values[binding.key] = binding.value
            except (OSError, UnicodeError):
                raise SettingsError("Unable to read the configuration file.") from None
        values.update(os.environ if environ is None else environ)
        return cls.from_mapping(values, root=path.parent)

    def readiness(self) -> dict:
        fields = {
            "KIS_PAPER_APP_KEY": self.app_key,
            "KIS_PAPER_APP_SECRET": self.app_secret,
            "KIS_PAPER_ACCOUNT_NUMBER": self.account_number,
            "KIS_PAPER_ACCOUNT_PRODUCT_CODE": self.account_product_code,
        }
        missing = [key for key, value in fields.items() if not value]
        return {
            "configured": not missing,
            "missing": missing,
            "stream_configured": not missing and bool(self.hts_id),
            "orders_enabled": self.orders_enabled,
            "mode": "kis_paper",
            "connected": False,
        }

    def require(self, *, account: bool = False, stream: bool = False, orders: bool = False):
        missing = []
        if not self.app_key:
            missing.append("KIS_PAPER_APP_KEY")
        if not self.app_secret:
            missing.append("KIS_PAPER_APP_SECRET")
        if account and not self.account_number:
            missing.append("KIS_PAPER_ACCOUNT_NUMBER")
        if account and not self.account_product_code:
            missing.append("KIS_PAPER_ACCOUNT_PRODUCT_CODE")
        if stream and not self.hts_id:
            missing.append("KIS_PAPER_HTS_ID")
        if missing:
            raise SettingsError("Missing configuration fields: " + ", ".join(missing))
        if orders and not self.orders_enabled:
            raise SettingsError("Paper order submission is disabled.")
