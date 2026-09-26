"""KIS paper stream protocol, subscription budget, and explicit reconnect runner.

WS notices are hints to reconciliation, not a durable exactly-once execution ledger.
No constructor opens a socket. Credentials and decrypted account records are never logged.
"""

import asyncio
import base64
import hashlib
import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from invest_lab.adapters.kis import decimal_string, integer, symbol_code
from invest_lab.adapters.settings import PAPER_WS_URL, KisSettings

TRADE_CHANNEL = "H0STCNT0"
BOOK_CHANNEL = "H0STASP0"
NOTICE_CHANNEL = "H0STCNI9"
SUPPORTED_CHANNELS = frozenset({TRADE_CHANNEL, BOOK_CHANNEL, NOTICE_CHANNEL})
KST = ZoneInfo("Asia/Seoul")

# Field order follows KIS's published current KRX/notice channel samples.
TRADE_FIELDS = (
    "MKSC_SHRN_ISCD STCK_CNTG_HOUR STCK_PRPR PRDY_VRSS_SIGN PRDY_VRSS PRDY_CTRT "
    "WGHN_AVRG_STCK_PRC STCK_OPRC STCK_HGPR STCK_LWPR ASKP1 BIDP1 CNTG_VOL ACML_VOL "
    "ACML_TR_PBMN SELN_CNTG_CSNU SHNU_CNTG_CSNU NTBY_CNTG_CSNU CTTR SELN_CNTG_SMTN "
    "SHNU_CNTG_SMTN CCLD_DVSN SHNU_RATE PRDY_VOL_VRSS_ACML_VOL_RATE OPRC_HOUR "
    "OPRC_VRSS_PRPR_SIGN OPRC_VRSS_PRPR HGPR_HOUR HGPR_VRSS_PRPR_SIGN HGPR_VRSS_PRPR "
    "LWPR_HOUR LWPR_VRSS_PRPR_SIGN LWPR_VRSS_PRPR BSOP_DATE NEW_MKOP_CLS_CODE "
    "TRHT_YN ASKP_RSQN1 BIDP_RSQN1 TOTAL_ASKP_RSQN TOTAL_BIDP_RSQN VOL_TNRT "
    "PRDY_SMNS_HOUR_ACML_VOL PRDY_SMNS_HOUR_ACML_VOL_RATE HOUR_CLS_CODE "
    "MRKT_TRTM_CLS_CODE VI_STND_PRC"
).split()
BOOK_FIELDS = (
    ["MKSC_SHRN_ISCD", "BSOP_HOUR", "HOUR_CLS_CODE"]
    + [f"ASKP{i}" for i in range(1, 11)]
    + [f"BIDP{i}" for i in range(1, 11)]
    + [f"ASKP_RSQN{i}" for i in range(1, 11)]
    + [f"BIDP_RSQN{i}" for i in range(1, 11)]
    + (
        "TOTAL_ASKP_RSQN TOTAL_BIDP_RSQN OVTM_TOTAL_ASKP_RSQN OVTM_TOTAL_BIDP_RSQN "
        "ANTC_CNPR ANTC_CNQN ANTC_VOL ANTC_CNTG_VRSS ANTC_CNTG_VRSS_SIGN ANTC_CNTG_PRDY_CTRT "
        "ACML_VOL TOTAL_ASKP_RSQN_ICDC TOTAL_BIDP_RSQN_ICDC OVTM_TOTAL_ASKP_ICDC "
        "OVTM_TOTAL_BIDP_ICDC STCK_DEAL_CLS_CODE"
    ).split()
)
NOTICE_FIELDS = (
    "CUST_ID ACNT_NO ODER_NO OODER_NO SELN_BYOV_CLS RCTF_CLS ODER_KIND ODER_COND "
    "STCK_SHRN_ISCD CNTG_QTY CNTG_UNPR STCK_CNTG_HOUR RFUS_YN CNTG_YN ACPT_YN BRNC_NO "
    "ODER_QTY ACNT_NAME ORD_COND_PRC ORD_EXG_GB POPUP_YN FILLER CRDT_CLS "
    "CRDT_LOAN_DATE CNTG_ISNM40 ODER_PRC"
).split()


class StreamProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class StreamEvent:
    kind: str
    channel: str
    observed_at: datetime
    symbol: str | None = None
    data: dict = field(default_factory=dict, repr=False)


class KisStreamParser:
    def __init__(self, settings: KisSettings, *, now=None):
        self.settings = settings
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._ciphers = {}

    def reset(self):
        self._ciphers.clear()

    def parse(self, message: str | bytes):
        try:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            if not isinstance(message, str) or len(message) > 1_048_576:
                raise StreamProtocolError("Invalid stream message size.")
            if message.startswith("{"):
                return self._control(json.loads(message))
            encrypted, channel, count_raw, payload = message.split("|", 3)
            if channel not in SUPPORTED_CHANNELS or encrypted not in {"0", "1"}:
                raise StreamProtocolError("Unsupported paper stream channel.")
            count = int(count_raw)
            if not 1 <= count <= 1000:
                raise StreamProtocolError("Invalid stream record count.")
            if encrypted == "1":
                key, iv = self._ciphers[channel]
                decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
                padded = (
                    decryptor.update(base64.b64decode(payload, validate=True))
                    + decryptor.finalize()
                )
                unpadder = padding.PKCS7(128).unpadder()
                payload = (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
            elif channel == NOTICE_CHANNEL:
                raise StreamProtocolError("Account notices must be encrypted.")
            columns = {
                TRADE_CHANNEL: TRADE_FIELDS,
                BOOK_CHANNEL: BOOK_FIELDS,
                NOTICE_CHANNEL: NOTICE_FIELDS,
            }[channel]
            values = payload.split("^")
            if len(values) != count * len(columns):
                raise StreamProtocolError("Stream schema or record count changed.")
            return [
                self._record(channel, dict(zip(columns, values[i : i + len(columns)], strict=True)))
                for i in range(0, len(values), len(columns))
            ]
        except StreamProtocolError:
            raise
        except Exception:
            # Neither the packet, decrypted record, key/IV, nor original exception is retained.
            raise StreamProtocolError("Invalid KIS stream packet.") from None

    def _control(self, message):
        header, body = message.get("header", {}), message.get("body", {})
        channel = header.get("tr_id")
        if channel == "PINGPONG":
            return [StreamEvent("ping", channel, self.now())]
        if channel not in SUPPORTED_CHANNELS:
            raise StreamProtocolError("Unsupported paper stream control channel.")
        key = header.get("tr_key")
        if not isinstance(key, str) or not key:
            raise StreamProtocolError("Subscription acknowledgment has no key.")
        if channel == NOTICE_CHANNEL and key != self.settings.hts_id:
            raise StreamProtocolError("Unexpected notice subscription.")
        success = body.get("rt_cd") == "0"
        output = body.get("output", {})
        if success and channel == NOTICE_CHANNEL and ("key" in output or "iv" in output):
            secret, iv = output["key"].encode(), output["iv"].encode()
            if len(secret) != 32 or len(iv) != 16:
                raise StreamProtocolError("Invalid notice cipher configuration.")
            self._ciphers[channel] = (secret, iv)
        if success and channel == NOTICE_CHANNEL and channel not in self._ciphers:
            raise StreamProtocolError("Notice subscription did not provide its cipher.")
        return [
            StreamEvent(
                "ack" if success else "subscription_error", channel, self.now(), data={"key": key}
            )
        ]

    @staticmethod
    def _event_time(day, time):
        return datetime.strptime(day + time, "%Y%m%d%H%M%S").replace(tzinfo=KST).isoformat()

    def _record(self, channel, row):
        at = self.now()
        if channel == TRADE_CHANNEL:
            symbol = symbol_code(row["MKSC_SHRN_ISCD"])
            price = decimal_string(row["STCK_PRPR"])
            if float(price) <= 0:
                raise StreamProtocolError("Invalid trade price.")
            data = {
                "symbol": symbol,
                "price": price,
                "observed_at": at.isoformat(),
                "source": "kis_paper_ws",
                "timestamp_basis": "exchange",
                "source_timestamp": self._event_time(row["BSOP_DATE"], row["STCK_CNTG_HOUR"]),
                "volume": integer(row["CNTG_VOL"]),
                "cumulative_volume": integer(row["ACML_VOL"]),
                "turnover": decimal_string(row["ACML_TR_PBMN"]),
                "venue": "KRX",
                "market_status": row["NEW_MKOP_CLS_CODE"],
                "halted": {"Y": True, "N": False}.get(row["TRHT_YN"]),
            }
            return StreamEvent("trade", channel, at, symbol, data)
        if channel == BOOK_CHANNEL:
            symbol = symbol_code(row["MKSC_SHRN_ISCD"])
            data = {
                "asks": [
                    {
                        "price": decimal_string(row[f"ASKP{i}"]),
                        "quantity": integer(row[f"ASKP_RSQN{i}"]),
                    }
                    for i in range(1, 11)
                ],
                "bids": [
                    {
                        "price": decimal_string(row[f"BIDP{i}"]),
                        "quantity": integer(row[f"BIDP_RSQN{i}"]),
                    }
                    for i in range(1, 11)
                ],
                "source_time": row["BSOP_HOUR"],
                "timestamp_basis": "received",
                "session_code": row["HOUR_CLS_CODE"],
                "venue": "KRX",
            }
            return StreamEvent("book", channel, at, symbol, data)
        expected = self.settings.account_number + self.settings.account_product_code
        if not expected or row["ACNT_NO"] != expected:
            raise StreamProtocolError("Notice account does not match the configured paper account.")
        if row["CNTG_YN"] not in {"1", "2"} or row["RFUS_YN"] not in {"0", "1"}:
            raise StreamProtocolError("Unknown account notice flags.")
        symbol = symbol_code(row["STCK_SHRN_ISCD"])
        kind = "fill" if row["CNTG_YN"] == "2" else "order_notice"
        # Notice has time-of-day but no trading date: receiving date is explicitly tentative.
        # REST reconciliation must confirm the date and cumulative fills before applying cash.
        data = {
            "broker_order_id": row["ODER_NO"],
            "original_order_id": row["OODER_NO"],
            "branch_id": row["BRNC_NO"],
            "side": {"01": "sell", "02": "buy"}[row["SELN_BYOV_CLS"]],
            "quantity": integer(row["ODER_QTY"]),
            "fill_quantity": integer(row["CNTG_QTY"]) if kind == "fill" else None,
            "fill_price": decimal_string(row["CNTG_UNPR"]) if kind == "fill" else None,
            "rejected": row["RFUS_YN"] == "1",
            "acceptance_code": row["ACPT_YN"],
            "revision_code": row["RCTF_CLS"],
            "source_time": row["STCK_CNTG_HOUR"],
            "date_confirmed": False,
            "trading_date_candidate": at.astimezone(KST).date().isoformat(),
            "event_fingerprint": hashlib.sha256(
                json.dumps(row, sort_keys=True).encode()
            ).hexdigest(),
            "requires_reconciliation": True,
            "venue_code": row["ORD_EXG_GB"],
        }
        return StreamEvent(kind, channel, at, symbol, data)


class SubscriptionManager:
    def __init__(self, settings: KisSettings, *, max_subscriptions=41):
        if not 1 <= max_subscriptions <= 41:
            raise ValueError("KIS supports at most 41 subscriptions per session.")
        self.settings, self.maximum = settings, max_subscriptions
        self.desired, self.acknowledged, self.pending = set(), set(), {}

    def configure(self, *, holdings=(), candidates=(), books=False):
        self.settings.require(account=True, stream=True)
        held = list(dict.fromkeys(symbol_code(value) for value in holdings))
        candidates = list(dict.fromkeys(symbol_code(value) for value in candidates))
        if len(held) > self.maximum - 1:
            raise ValueError("Held instruments exceed the available stream budget.")
        chosen = list(dict.fromkeys(held + candidates))[: self.maximum - 1]
        desired = {(NOTICE_CHANNEL, self.settings.hts_id)}
        desired.update((TRADE_CHANNEL, symbol) for symbol in chosen)
        if books:
            desired.update(
                (BOOK_CHANNEL, symbol) for symbol in chosen[: self.maximum - len(desired)]
            )
        self.desired = desired
        return {
            "subscribed_symbols": chosen,
            "excluded_candidates": [symbol for symbol in candidates if symbol not in chosen],
            "count": len(desired),
            "limit": self.maximum,
        }

    def disconnected(self):
        self.acknowledged.clear()
        self.pending.clear()

    def requests(self, approval_key):
        # Free slots first; additions wait for unsubscribe ACK to avoid transient overflow.
        removals = self.acknowledged - self.desired
        targets = [(key, "2") for key in sorted(removals) if key not in self.pending]
        if not removals and not any(action == "2" for action in self.pending.values()):
            targets += [
                (key, "1")
                for key in sorted(self.desired - self.acknowledged)
                if key not in self.pending
            ]
        result = []
        for (channel, key), action in targets:
            self.pending[channel, key] = action
            result.append(
                {
                    "header": {
                        "approval_key": approval_key,
                        "custtype": "P",
                        "tr_type": action,
                        "content-type": "utf-8",
                    },
                    "body": {"input": {"tr_id": channel, "tr_key": key}},
                }
            )
        return result

    def acknowledge(self, event: StreamEvent):
        key = (event.channel, event.data.get("key"))
        action = self.pending.pop(key, None)
        if action is None:
            raise StreamProtocolError("Unsolicited subscription acknowledgment.")
        if event.kind != "ack":
            raise StreamProtocolError("Subscription was rejected.")
        if action == "1":
            self.acknowledged.add(key)
        else:
            self.acknowledged.discard(key)

    @property
    def ready(self):
        return bool(self.desired) and self.desired == self.acknowledged and not self.pending


class KisPaperStream:
    """An account worker calls run(); read-only HTTP/UI startup never starts it."""

    def __init__(
        self, client, *, on_event, reconcile, connect=None, now=None, receive_timeout=45.0
    ):
        self.client = client
        self.settings = client.settings
        self.parser = KisStreamParser(self.settings, now=now)
        self.subscriptions = SubscriptionManager(self.settings)
        self.on_event, self.reconcile, self.connect = on_event, reconcile, connect
        self.receive_timeout = receive_timeout
        self.state = {
            "connected": False,
            "subscribed": False,
            "reconciled": False,
            "market_data_received": False,
            "account_notice_received": False,
            "last_event_at": None,
            "last_error": None,
            "reconnects": 0,
        }

    async def _call(self, function, *args):
        if inspect.iscoroutinefunction(function):
            return await function(*args)
        value = await asyncio.to_thread(function, *args)
        return await value if inspect.isawaitable(value) else value

    async def run(self, stop: asyncio.Event):
        self.settings.require(account=True, stream=True)
        if not self.subscriptions.desired:
            self.subscriptions.configure()
        if self.connect is None:
            from websockets.asyncio.client import connect
        else:
            connect = self.connect
        delay = 1
        while not stop.is_set():
            reconciliation_task = None
            self.parser.reset()
            self.subscriptions.disconnected()
            self.state.update(
                connected=False,
                subscribed=False,
                reconciled=False,
                market_data_received=False,
                account_notice_received=False,
            )
            try:
                approval = await self._call(self.client.approval_key)
                async with connect(
                    PAPER_WS_URL,
                    ping_interval=None,
                    max_size=1_048_576,
                    open_timeout=15,
                    close_timeout=5,
                ) as socket:
                    self.state.update(connected=True, last_error=None)
                    connected_at = asyncio.get_running_loop().time()
                    awaiting_since = connected_at
                    while not stop.is_set():
                        if reconciliation_task is not None and reconciliation_task.done():
                            outcome = reconciliation_task.result()
                            if outcome is False:
                                raise StreamProtocolError("Reconciliation did not succeed.")
                            self.state["reconciled"] = True
                            reconciliation_task = None
                            delay = 1
                        current_time = asyncio.get_running_loop().time()
                        if not self.subscriptions.ready:
                            self.state.update(subscribed=False, reconciled=False)
                            if awaiting_since is None:
                                awaiting_since = current_time
                            if current_time - awaiting_since > self.receive_timeout:
                                raise StreamProtocolError("Subscription acknowledgment timed out.")
                        else:
                            awaiting_since = None
                        for request in self.subscriptions.requests(approval):
                            await socket.send(json.dumps(request))
                            await asyncio.sleep(0.1)
                        try:
                            packet = await asyncio.wait_for(socket.recv(), timeout=1)
                        except TimeoutError:
                            if (
                                asyncio.get_running_loop().time() - connected_at
                                > self.receive_timeout
                            ):
                                raise StreamProtocolError(
                                    "Stream is stale or awaiting acknowledgment."
                                )
                            continue
                        connected_at = asyncio.get_running_loop().time()
                        for event in self.parser.parse(packet):
                            if event.kind == "ping":
                                await socket.pong(
                                    packet.encode() if isinstance(packet, str) else packet
                                )
                                continue
                            if event.kind in {"ack", "subscription_error"}:
                                self.subscriptions.acknowledge(event)
                                if self.subscriptions.ready:
                                    self.state["subscribed"] = True
                                    if reconciliation_task is None:
                                        reconciliation_task = asyncio.create_task(
                                            self._call(self.reconcile)
                                        )
                                continue
                            self.state["last_event_at"] = event.observed_at.isoformat()
                            if event.kind in {"trade", "book"}:
                                self.state["market_data_received"] = True
                            if event.kind in {"fill", "order_notice"}:
                                self.state["account_notice_received"] = True
                            await self._call(self.on_event, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.state["last_error"] = "STREAM_INTERRUPTED_RECONCILIATION_REQUIRED"
                self.state["reconnects"] += 1
            finally:
                if reconciliation_task is not None:
                    reconciliation_task.cancel()
                    try:
                        await reconciliation_task
                    except (asyncio.CancelledError, Exception):
                        pass
                self.state.update(connected=False, subscribed=False, reconciled=False)
                self.parser.reset()
                self.subscriptions.disconnected()
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(delay * 2, 30)
