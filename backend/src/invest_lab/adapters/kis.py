"""KIS paper REST boundary; every network operation is explicit and injectable."""

import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from invest_lab.adapters.settings import PAPER_BASE_URL, KisSettings
from invest_lab.domain.errors import TradingBlocked

KST = ZoneInfo("Asia/Seoul")
PREFIX = "/uapi/domestic-stock/v1/"
SYMBOL = re.compile(r"[A-Z0-9]{6}")


class KisError(Exception):
    def __init__(self, code: str, *, retryable=False, ambiguous=False, http_status=None):
        # Never retain a response body, request, secret-bearing URL or broker message.
        self.code = code if re.fullmatch(r"[A-Za-z0-9_]{1,50}", code) else "KIS_ERROR"
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.http_status = http_status
        super().__init__(self.code)


class BrokerRejected(KisError, TradingBlocked):
    pass


KisBusinessError = BrokerRejected


class AmbiguousOrder(KisError):
    def __init__(self, code="ORDER_RESULT_UNKNOWN", **kwargs):
        super().__init__(code, ambiguous=True, **kwargs)


class BrokerDataError(KisError):
    pass


def symbol_code(value: str) -> str:
    if not isinstance(value, str) or not SYMBOL.fullmatch(value):
        raise ValueError("A six-character uppercase instrument code is required.")
    return value


def decimal_string(value, field="number", *, negative=False) -> str:
    try:
        if isinstance(value, bool) or value is None or str(value).strip() == "":
            raise InvalidOperation
        text = str(value).strip()
        if len(text) > 100 or (
            "," in text and not re.fullmatch(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?", text)
        ):
            raise InvalidOperation
        number = Decimal(text.replace(",", ""))
        if not number.is_finite() or (not negative and number < 0):
            raise InvalidOperation
        if not -18 <= number.as_tuple().exponent <= 30 or number.adjusted() > 30:
            raise InvalidOperation
        return format(number, "f")
    except (InvalidOperation, ValueError):
        raise BrokerDataError("INVALID_NUMERIC_FIELD") from None


def integer(value) -> int:
    number = Decimal(decimal_string(value))
    if number != number.to_integral_value():
        raise BrokerDataError("INVALID_INTEGER_FIELD")
    return int(number)


def _date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValueError("A valid date is required.") from None


class KisCoordinator:
    """Atomic token cache and shared per-account rate reservations across processes."""

    def __init__(self, path: Path, interval=1.25, *, now=time.time, sleep=time.sleep):
        self.path, self.interval, self.now, self.sleep = Path(path), interval, now, sleep
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS kis_cache (
                    scope TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kis_rate (scope TEXT PRIMARY KEY, next_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS kis_token_lease (
                    scope TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def wait(self, scope: str):
        while True:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT next_at FROM kis_rate WHERE scope=?", (scope,)
                ).fetchone()
                now = self.now()
                delay = max(0, (row[0] if row else now) - now)
                if delay == 0:
                    conn.execute(
                        "INSERT INTO kis_rate VALUES (?,?) ON CONFLICT(scope) "
                        "DO UPDATE SET next_at=excluded.next_at",
                        (scope, now + self.interval),
                    )
                    return
            # Recheck after waking: a late process may not consume an old reserved slot.
            self.sleep(delay)

    def cached_token(self, scope: str):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT token,expires FROM kis_cache WHERE scope=?", (scope,)
            ).fetchone()
        return row[0] if row and row[1] > self.now() + 60 else None

    def invalidate(self, scope: str):
        with self.connection() as conn:
            conn.execute("DELETE FROM kis_cache WHERE scope=?", (scope,))

    def token(self, scope: str, issue: Callable[[], tuple[str, float]]) -> str:
        owner = str(uuid4())
        deadline = self.now() + 140
        while self.now() < deadline:
            cached = self.cached_token(scope)
            if cached:
                return cached
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM kis_token_lease WHERE expires<=?", (self.now(),))
                acquired = conn.execute(
                    "INSERT OR IGNORE INTO kis_token_lease VALUES (?,?,?)",
                    (scope, owner, self.now() + 130),
                ).rowcount
            if not acquired:
                self.sleep(0.1)
                continue
            try:
                # Another process may have completed between our first read and acquisition.
                cached = self.cached_token(scope)
                if cached:
                    return cached
                token, expires = issue()
                with self.connection() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    lease = conn.execute(
                        "SELECT owner,expires FROM kis_token_lease WHERE scope=?", (scope,)
                    ).fetchone()
                    if lease is None or lease[0] != owner or lease[1] <= self.now():
                        raise KisError("TOKEN_ISSUE_LEASE_EXPIRED", retryable=True)
                    conn.execute(
                        "INSERT INTO kis_cache VALUES (?,?,?) ON CONFLICT(scope) "
                        "DO UPDATE SET token=excluded.token,expires=excluded.expires",
                        (scope, token, expires),
                    )
                return token
            finally:
                with self.connection() as conn:
                    conn.execute(
                        "DELETE FROM kis_token_lease WHERE scope=? AND owner=?", (scope, owner)
                    )
        raise KisError("TOKEN_ISSUE_BUSY", retryable=True)


class KisPaperClient:
    source = "kis_paper"
    capabilities = {
        "quote": True,
        "bars": True,
        "account": True,
        "capacity": True,
        "orders": True,
        "buy": True,
        "sell": True,
        "amend": True,
        "cancel": True,
        "sell_capacity_endpoint": False,
        "cancelable_orders_endpoint": False,
        "holiday_endpoint": False,
        "ws_market_status": False,
        "venues": ("KRX",),
        "order_types": ("limit", "market"),
    }

    def __init__(
        self,
        settings: KisSettings,
        *,
        transport=None,
        http_client=None,
        coordinator=None,
        now=None,
        sleep=time.sleep,
        max_pages=1000,
    ):
        self.settings = settings
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.max_pages = max_pages
        self.coordinator = coordinator or KisCoordinator(
            settings.cache_path, settings.request_interval, sleep=sleep
        )
        self._http = http_client or httpx.Client(
            transport=transport, timeout=settings.timeout, follow_redirects=False, trust_env=False
        )
        self._owns_http = http_client is None
        # Account is the shared throttle scope; a different app key cannot bypass it.
        identity = settings.account_number or settings.app_key
        self._rate_scope = hashlib.sha256(("paper:" + identity).encode()).hexdigest()
        self._token_scope = hashlib.sha256((PAPER_BASE_URL + settings.app_key).encode()).hexdigest()

    def __repr__(self):
        return "KisPaperClient(mode='kis_paper')"

    def close(self):
        if self._owns_http:
            self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _issue_token(self):
        self.settings.require()
        self.coordinator.wait(self._rate_scope)
        try:
            response = self._http.post(
                PAPER_BASE_URL + "/oauth2/tokenP",
                follow_redirects=False,
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.settings.app_key,
                    "appsecret": self.settings.app_secret,
                },
            )
            body = response.json()
            if response.status_code != 200 or not isinstance(body, dict):
                raise KisError("TOKEN_ISSUE_FAILED", http_status=response.status_code)
            token = body.get("access_token")
            if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
                raise KisError("TOKEN_RESPONSE_INVALID")
            expires_raw = body.get("access_token_token_expired")
            if expires_raw:
                expires = (
                    datetime.strptime(expires_raw, "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=KST)
                    .timestamp()
                )
            else:
                lifetime = integer(body.get("expires_in"))
                expires = self.now().timestamp() + lifetime
            if expires <= self.now().timestamp() + 60:
                raise KisError("TOKEN_EXPIRED")
            return token, expires
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise KisError("TOKEN_ISSUE_FAILED", retryable=True) from None

    def _request(self, path, tr_id, params, *, post=False, continuation=""):
        self.settings.require()
        token = self.coordinator.token(self._token_scope, self._issue_token)
        attempts = 1 if post else 3
        for attempt in range(attempts):
            self.coordinator.wait(self._rate_scope)
            headers = {
                "authorization": "Bearer " + token,
                "appkey": self.settings.app_key,
                "appsecret": self.settings.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
                "tr_cont": continuation,
                "content-type": "application/json",
            }
            try:
                if post:
                    response = self._http.post(
                        PAPER_BASE_URL + path, headers=headers, json=params, follow_redirects=False
                    )
                else:
                    response = self._http.get(
                        PAPER_BASE_URL + path,
                        headers=headers,
                        params=params,
                        follow_redirects=False,
                    )
            except httpx.HTTPError:
                if post:
                    raise AmbiguousOrder() from None
                if attempt + 1 < attempts:
                    self.sleep(2**attempt)
                    continue
                raise KisError("KIS_UNAVAILABLE", retryable=True) from None
            try:
                body = response.json()
            except ValueError:
                if post:
                    raise AmbiguousOrder() from None
                raise BrokerDataError("INVALID_BROKER_RESPONSE") from None
            if not isinstance(body, dict):
                if post:
                    raise AmbiguousOrder()
                raise BrokerDataError("INVALID_BROKER_RESPONSE")
            if response.status_code == 200 and body.get("rt_cd") == "0":
                return body, response.headers.get("tr_cont", "").strip()
            code = str(body.get("msg_cd") or "KIS_REQUEST_FAILED")
            retryable = response.status_code in {429, 502, 503, 504} or code in {
                "EGW00201",
                "EGW00215",
                "EGW00123",
                "EGW00121",
            }
            if post:
                if response.status_code >= 500 or body.get("rt_cd") != "1":
                    raise AmbiguousOrder(http_status=response.status_code)
                raise BrokerRejected(code, http_status=response.status_code)
            if code in {"EGW00123", "EGW00121"} and attempt + 1 < attempts:
                self.coordinator.invalidate(self._token_scope)
                token = self.coordinator.token(self._token_scope, self._issue_token)
            elif not retryable or attempt + 1 == attempts:
                raise KisError(code, retryable=retryable, http_status=response.status_code)
            self.sleep(2**attempt)
        raise KisError("KIS_UNAVAILABLE", retryable=True)

    def _account_params(self):
        self.settings.require(account=True)
        return {
            "CANO": self.settings.account_number,
            "ACNT_PRDT_CD": self.settings.account_product_code,
        }

    def _pages(self, path, tr_id, params):
        cursor = ("", "")
        seen = set()
        continuation = ""
        for _ in range(self.max_pages):
            if cursor in seen:
                raise BrokerDataError("REPEATED_PAGE_CURSOR")
            seen.add(cursor)
            body, more = self._request(
                path,
                tr_id,
                {**params, "CTX_AREA_FK100": cursor[0], "CTX_AREA_NK100": cursor[1]},
                continuation=continuation,
            )
            yield body
            if more not in {"M", "F"}:
                return
            cursor = (body.get("ctx_area_fk100", ""), body.get("ctx_area_nk100", ""))
            if not all(isinstance(part, str) for part in cursor):
                raise BrokerDataError("INVALID_PAGE_CURSOR")
            continuation = "N"
        raise BrokerDataError("PAGE_LIMIT_EXCEEDED")

    def quote(self, symbol):
        symbol_code(symbol)
        body, _ = self._request(
            PREFIX + "quotations/inquire-price",
            "FHKST01010100",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            },
        )
        row = body.get("output")
        if not isinstance(row, dict):
            raise BrokerDataError("MISSING_QUOTE")
        price = decimal_string(row.get("stck_prpr"))
        if Decimal(price) <= 0:
            raise BrokerDataError("INVALID_QUOTE_PRICE")
        result = {
            "symbol": symbol,
            "price": price,
            "observed_at": self.now().isoformat(),
            "source": self.source,
            "timestamp_basis": "received",
            "source_timestamp": None,
            "venue": "KRX",
            "market_status": row.get("iscd_stat_cls_code"),
        }
        for source, target in (
            ("aspr_unit", "tick_size"),
            ("stck_mxpr", "upper_limit"),
            ("stck_llam", "lower_limit"),
        ):
            value = row.get(source)
            result[target] = decimal_string(value) if value not in (None, "") else None
            if result[target] is not None and Decimal(result[target]) <= 0:
                raise BrokerDataError("INVALID_QUOTE_BAND")
        if result["upper_limit"] is not None and result["lower_limit"] is not None:
            if (
                not Decimal(result["lower_limit"])
                <= Decimal(price)
                <= Decimal(result["upper_limit"])
            ):
                raise BrokerDataError("QUOTE_OUTSIDE_BAND")
        return result

    def bars(self, symbol, start, end, adjusted=False):
        symbol_code(symbol)
        start, end = _date(start), _date(end)
        if start > end:
            raise ValueError("History start must not follow end.")
        result = {}
        # A 99-calendar-day window cannot contain more than the endpoint's 100 bars.
        cursor = start
        while cursor <= end:
            finish = min(end, cursor + timedelta(days=98))
            body, _ = self._request(
                PREFIX + "quotations/inquire-daily-itemchartprice",
                "FHKST03010100",
                {
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": symbol,
                    "FID_INPUT_DATE_1": cursor.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": finish.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
                },
            )
            rows = body.get("output2")
            if not isinstance(rows, list):
                raise BrokerDataError("MISSING_BAR_HISTORY")
            for row in rows:
                if not row or not row.get("stck_bsop_date"):
                    continue
                try:
                    session = datetime.strptime(row["stck_bsop_date"], "%Y%m%d").date()
                except (KeyError, ValueError, TypeError):
                    raise BrokerDataError("INVALID_BAR_SESSION") from None
                if not cursor <= session <= finish:
                    raise BrokerDataError("OUT_OF_RANGE_BAR")
                item = {
                    "session": session.isoformat(),
                    "symbol": symbol,
                    "source": self.source,
                    "adjusted": adjusted,
                    "venue": "KRX",
                }
                for source, target in (
                    ("stck_oprc", "open"),
                    ("stck_hgpr", "high"),
                    ("stck_lwpr", "low"),
                    ("stck_clpr", "close"),
                ):
                    item[target] = decimal_string(row.get(source))
                item["volume"] = integer(row.get("acml_vol"))
                item["turnover"] = decimal_string(row.get("acml_tr_pbmn"))
                if session.isoformat() in result and result[session.isoformat()] != item:
                    raise BrokerDataError("CONFLICTING_BAR")
                result[session.isoformat()] = item
            cursor = finish + timedelta(days=1)
        return [result[key] for key in sorted(result)]

    def capacity(self, symbol, price=None, order_type="limit"):
        if symbol:
            symbol_code(symbol)
        if order_type not in {"limit", "market"}:
            raise ValueError("Unsupported order type.")
        body, _ = self._request(
            PREFIX + "trading/inquire-psbl-order",
            "VTTC8908R",
            {
                **self._account_params(),
                "PDNO": symbol,
                "ORD_UNPR": ""
                if order_type == "market" or price is None
                else decimal_string(price),
                "ORD_DVSN": "01" if order_type == "market" else "00",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        output = body.get("output")
        if not isinstance(output, dict):
            raise BrokerDataError("MISSING_CAPACITY")
        return {
            "amount": decimal_string(output.get("nrcvb_buy_amt")),
            "quantity": integer(output.get("nrcvb_buy_qty")) if symbol else None,
            "observed_at": self.now().isoformat(),
            "source": self.source,
        }

    def account(self):
        params = {
            **self._account_params(),
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
        }
        positions = {}
        summary = None
        balance_observed_at = None
        for page in self._pages(PREFIX + "trading/inquire-balance", "VTTC8434R", params):
            if balance_observed_at is None:
                balance_observed_at = self.now().isoformat()
            rows = page.get("output1")
            outputs = page.get("output2")
            if not isinstance(rows, list) or not isinstance(outputs, list) or not outputs:
                raise BrokerDataError("MISSING_ACCOUNT_DATA")
            current_summary = outputs[0]
            if not isinstance(current_summary, dict):
                raise BrokerDataError("INVALID_ACCOUNT_SUMMARY")
            if summary is not None and any(
                str(summary.get(key)) != str(current_summary.get(key))
                for key in ("dnca_tot_amt", "tot_evlu_amt", "scts_evlu_amt")
            ):
                raise BrokerDataError("ACCOUNT_CHANGED_DURING_PAGING")
            summary = current_summary
            for row in rows:
                qty = integer(row.get("hldg_qty"))
                if qty == 0:
                    continue
                symbol = symbol_code(row.get("pdno"))
                if symbol in positions:
                    raise BrokerDataError("DUPLICATE_ACCOUNT_POSITION")
                sellable = integer(row.get("ord_psbl_qty"))
                if sellable > qty:
                    raise BrokerDataError("INCONSISTENT_SELLABLE_QUANTITY")
                positions[symbol] = {
                    "symbol": symbol,
                    "name": row.get("prdt_name", symbol),
                    "quantity": qty,
                    "sellable_quantity": sellable,
                    "average_price": decimal_string(row.get("pchs_avg_pric")),
                    "price": decimal_string(row.get("prpr")),
                    "value": decimal_string(row.get("evlu_amt")),
                    "profit": decimal_string(row.get("evlu_pfls_amt"), negative=True),
                }
        if summary is None:
            raise BrokerDataError("MISSING_ACCOUNT_SUMMARY")
        buying = self.capacity("", None)
        return {
            "cash": decimal_string(summary.get("dnca_tot_amt"), negative=True),
            "buying_power": buying["amount"],
            "equity": decimal_string(summary.get("tot_evlu_amt"), negative=True),
            "holdings_value": decimal_string(summary.get("scts_evlu_amt")),
            "settlement_d1": decimal_string(summary.get("nxdy_excc_amt"), negative=True),
            "settlement_d2": decimal_string(summary.get("prvs_rcdl_excc_amt"), negative=True),
            "positions": list(positions.values()),
            "observed_at": balance_observed_at,
            "source": self.source,
            "mode": "kis_paper",
            "buying_power_observed_at": buying["observed_at"],
            "snapshot_completed_at": self.now().isoformat(),
            "timestamp_basis": "received",
        }

    def orders(self, start, end):
        start, end = _date(start), _date(end)
        if start > end:
            raise ValueError("Order history start must not follow end.")
        # Calendar-month boundary, not an approximate 90-day cutoff.
        today = self.now().astimezone(KST).date()
        month_index = today.year * 12 + today.month - 1 - 3
        year, month = divmod(month_index, 12)
        from calendar import monthrange

        boundary = date(year, month + 1, min(today.day, monthrange(year, month + 1)[1]))
        ranges = []
        if start < boundary:
            ranges.append((start, min(end, boundary - timedelta(days=1)), "VTSC9215R"))
        if end >= boundary:
            ranges.append((max(start, boundary), end, "VTTC0081R"))
        result = {}
        for left, right, tr in ranges:
            params = {
                **self._account_params(),
                "INQR_STRT_DT": left.strftime("%Y%m%d"),
                "INQR_END_DT": right.strftime("%Y%m%d"),
                "SLL_BUY_DVSN_CD": "00",
                "CCLD_DVSN": "00",
                "INQR_DVSN": "01",
                "INQR_DVSN_3": "00",
                "PDNO": "",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_1": "",
                "EXCG_ID_DVSN_CD": "KRX",
            }
            for page in self._pages(PREFIX + "trading/inquire-daily-ccld", tr, params):
                rows = page.get("output1")
                if not isinstance(rows, list):
                    raise BrokerDataError("MISSING_ORDER_HISTORY")
                for row in rows:
                    normalized = self._order_row(row)
                    if not left.isoformat() <= normalized["trading_date"] <= right.isoformat():
                        raise BrokerDataError("OUT_OF_RANGE_ORDER")
                    key = (normalized["trading_date"], normalized["broker_order_id"])
                    if key in result and result[key] != normalized:
                        raise BrokerDataError("ORDER_CHANGED_DURING_PAGING")
                    result[key] = normalized
        return list(result.values())

    def _order_row(self, row):
        try:
            trading_date = datetime.strptime(row["ord_dt"], "%Y%m%d").date().isoformat()
            side = {"01": "sell", "02": "buy"}[row["sll_buy_dvsn_cd"]]
            filled, remaining = integer(row["tot_ccld_qty"]), integer(row["rmn_qty"])
            cancelled, rejected = integer(row["cncl_cfrm_qty"]), integer(row["rjct_qty"])
            quantity = integer(row["ord_qty"])
            if quantity <= 0 or filled > quantity or remaining > quantity:
                raise BrokerDataError("INVALID_ORDER_QUANTITY")
            kind = {"00": "limit", "01": "market"}.get(row.get("ord_dvsn_cd"), "unsupported")
            order_time = (
                datetime.strptime(row["ord_dt"] + row["ord_tmd"], "%Y%m%d%H%M%S")
                .replace(tzinfo=KST)
                .isoformat()
            )
            status = "partially_filled" if filled else "accepted"
            if remaining == 0:
                status = "cancelled" if cancelled else "rejected" if rejected else "filled"
            return {
                "broker_order_id": self._identifier(row.get("odno")),
                "original_order_id": str(row.get("orgn_odno") or "").strip(),
                "branch_id": self._identifier(row.get("ord_gno_brno")),
                "trading_date": trading_date,
                "symbol": symbol_code(row["pdno"]),
                "side": side,
                "quantity": quantity,
                "filled_quantity": filled,
                "filled_amount": decimal_string(row["tot_ccld_amt"]),
                "cancelled_quantity": cancelled,
                "cancel_flag": str(row.get("cncl_yn", "")),
                "remaining_quantity": remaining,
                "rejected_quantity": rejected,
                "status": status,
                "price": decimal_string(row["ord_unpr"]),
                "order_type": kind,
                "limit_price": decimal_string(row["ord_unpr"]) if kind == "limit" else None,
                "order_type_code": str(row.get("ord_dvsn_cd", "")),
                "ordered_at": order_time,
                "source": self.source,
                "venue": "KRX",
            }
        except (KeyError, TypeError, ValueError):
            raise BrokerDataError("INVALID_ORDER_HISTORY") from None

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 30:
            raise BrokerDataError("INVALID_BROKER_IDENTIFIER")
        return value.strip()

    def _receipt(self, body):
        try:
            row = {key.lower(): value for key, value in body["output"].items()}
            today = self.now().astimezone(KST).date()
            ordered_at = (
                datetime.strptime(
                    today.strftime("%Y%m%d") + self._identifier(row.get("ord_tmd")),
                    "%Y%m%d%H%M%S",
                )
                .replace(tzinfo=KST)
                .isoformat()
            )
            return {
                "broker_order_id": self._identifier(row.get("odno")),
                "branch_id": self._identifier(row.get("krx_fwdg_ord_orgno")),
                "ordered_at": ordered_at,
                "trading_date": today.isoformat(),
                "source": self.source,
                "status": "accepted",
            }
        except (KeyError, AttributeError, TypeError, ValueError, BrokerDataError):
            # A success response without its order ID still may have placed an order.
            raise AmbiguousOrder("ORDER_RECEIPT_INVALID") from None

    def place_order(self, payload):
        self.settings.require(account=True, orders=True)
        symbol = symbol_code(payload["symbol"])
        side, kind, quantity = payload["side"], payload["order_type"], payload["quantity"]
        if side not in {"buy", "sell"} or kind not in {"limit", "market"}:
            raise ValueError("Unsupported paper order.")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("Order quantity must be a positive integer.")
        price = "0" if kind == "market" else decimal_string(payload.get("limit_price"))
        if kind == "limit" and Decimal(price) <= 0:
            raise ValueError("A positive limit price is required.")
        body, _ = self._request(
            PREFIX + "trading/order-cash",
            "VTTC0012U" if side == "buy" else "VTTC0011U",
            {
                **self._account_params(),
                "PDNO": symbol,
                "ORD_QTY": str(quantity),
                "ORD_UNPR": price,
                "ORD_DVSN": "00" if kind == "limit" else "01",
                "EXCG_ID_DVSN_CD": "KRX",
                "SLL_TYPE": "01" if side == "sell" else "",
                "CNDT_PRIC": "",
            },
            post=True,
        )
        return self._receipt(body)

    def cancel_order(self, order, quantity, price=None):
        self.settings.require(account=True, orders=True)
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("Cancellation/amendment quantity must be a positive integer.")
        remaining = order.get("remaining_quantity")
        if remaining is not None and quantity > remaining:
            raise ValueError("Quantity exceeds the reconciled remainder.")
        amend = price is not None
        amount = decimal_string(price) if amend else "0"
        if amend and Decimal(amount) <= 0:
            raise ValueError("A positive amendment price is required.")
        body, _ = self._request(
            PREFIX + "trading/order-rvsecncl",
            "VTTC0013U",
            {
                **self._account_params(),
                "KRX_FWDG_ORD_ORGNO": self._identifier(order.get("branch_id")),
                "ORGN_ODNO": self._identifier(order.get("broker_order_id")),
                "ORD_DVSN": "00" if amend or order.get("order_type") != "market" else "01",
                "RVSE_CNCL_DVSN_CD": "01" if amend else "02",
                "ORD_QTY": str(quantity),
                "ORD_UNPR": amount,
                "QTY_ALL_ORD_YN": "N",
                "EXCG_ID_DVSN_CD": "KRX",
            },
            post=True,
        )
        return self._receipt(body)

    def approval_key(self):
        """Obtain a WS credential only when a stream is explicitly started."""
        self.settings.require()
        self.coordinator.wait(self._rate_scope)
        try:
            response = self._http.post(
                PAPER_BASE_URL + "/oauth2/Approval",
                follow_redirects=False,
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.settings.app_key,
                    "secretkey": self.settings.app_secret,
                },
            )
            body = response.json()
            key = body.get("approval_key")
            if response.status_code != 200 or not isinstance(key, str) or not key:
                raise KisError("WS_APPROVAL_FAILED")
            return key
        except (httpx.HTTPError, ValueError, AttributeError):
            raise KisError("WS_APPROVAL_FAILED") from None
