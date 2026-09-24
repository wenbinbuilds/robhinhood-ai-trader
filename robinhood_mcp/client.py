"""Persistent direct client for Robinhood's official MCP endpoint.

The client deliberately exposes no generic public broker-operation method.
Every invocation is checked against a fixed factual/scanner-only allowlist and
against the server's live tool inventory before it reaches the SDK.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import Future
from contextlib import AsyncExitStack
from typing import Any, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from robinhood_mcp.auth import KeychainTokenStorage, LocalOAuthCallback, make_oauth_provider
from robinhood_mcp.errors import (
    DirectMcpUnavailable,
    RobinhoodAuthenticationRequired,
    RobinhoodResponseError,
    ToolDiscoveryError,
    UnsafeToolError,
)
from robinhood_mcp.models import McpTool, ToolCall


READ_ONLY_TOOLS = frozenset(
    {
        "get_accounts",
        "get_portfolio",
        "get_realized_pnl",
        "get_pnl_trade_history",
        "get_equity_positions",
        "get_equity_orders",
        "get_equity_quotes",
        "get_equity_historicals",
        "get_equity_tradability",
        "get_scans",
        "get_scanner_filter_specs",
    }
)
PROJECT_SCANNER_TOOLS = frozenset(
    {"create_scan", "update_scan_filters", "update_scan_config", "run_scan"}
)
ALLOWED_TOOLS = READ_ONLY_TOOLS | PROJECT_SCANNER_TOOLS

# Defense in depth: any discovered or requested name containing one of these is
# rejected even if a future edit accidentally adds it to the allowlist.
FORBIDDEN_TOOL_MARKERS = frozenset(
    {
        "order_place", "place_order", "place_equity", "place_option", "place_crypto",
        "preview", "review_order", "cancel", "replace_order", "modify_order",
        "close_position", "submit", "transfer", "account_setting",
    }
)


def _unsafe_name(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in FORBIDDEN_TOOL_MARKERS)


def _contains_exception(value: BaseException, kind: type[BaseException]) -> bool:
    if isinstance(value, kind):
        return True
    nested = getattr(value, "exceptions", ())
    if isinstance(nested, Sequence) and any(
        isinstance(item, BaseException) and _contains_exception(item, kind)
        for item in nested
    ):
        return True
    cause = value.__cause__ or value.__context__
    return isinstance(cause, BaseException) and _contains_exception(cause, kind)


def _schema(tool: McpTool) -> Mapping[str, Any]:
    return tool.input_schema if isinstance(tool.input_schema, Mapping) else {}


def _properties(tool: McpTool) -> Mapping[str, Any]:
    value = _schema(tool).get("properties", {})
    return value if isinstance(value, Mapping) else {}


def _required(tool: McpTool) -> set[str]:
    value = _schema(tool).get("required", [])
    return {str(item) for item in value} if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else set()


def _is_array_schema(spec: Any) -> bool:
    if not isinstance(spec, Mapping):
        return False
    kind = spec.get("type")
    return (
        kind == "array"
        or (isinstance(kind, Sequence) and not isinstance(kind, (str, bytes)) and "array" in kind)
        or isinstance(spec.get("items"), Mapping)
    )


def _first_property(tool: McpTool, names: Iterable[str]) -> str | None:
    properties = _properties(tool)
    lower = {str(name).lower(): str(name) for name in properties}
    for candidate in names:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    return None


def _enum_choice(spec: Any, preferences: Sequence[str]) -> Any:
    if not isinstance(spec, Mapping):
        return None
    if "const" in spec:
        return spec["const"]
    if "default" in spec:
        default = spec["default"]
        if default is not None:
            return default
    values = spec.get("enum")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return None
    lookup = {str(value).lower().replace("_", "").replace("-", ""): value for value in values}
    for preference in preferences:
        normalized = preference.lower().replace("_", "").replace("-", "")
        if normalized in lookup:
            return lookup[normalized]
    return None


def quote_arguments(tool: McpTool, symbols: Sequence[str]) -> dict[str, Any]:
    """Create arguments using only property names discovered in the live schema."""

    key = _first_property(tool, ("symbols", "symbol", "tickers", "ticker"))
    if key is None:
        raise ToolDiscoveryError("quote tool schema has no supported symbol field")
    spec = _properties(tool).get(key, {})
    array_shaped = _is_array_schema(spec)
    value: Any = list(symbols) if array_shaped else symbols[0]
    arguments = {key: value}
    unknown = _required(tool) - arguments.keys()
    if unknown:
        raise ToolDiscoveryError("quote tool has unsupported required parameters")
    return arguments


def historical_arguments(
    tool: McpTool, symbol: str, *, now: datetime | None = None
) -> dict[str, Any]:
    properties = _properties(tool)
    symbol_key = _first_property(tool, ("symbol", "symbols", "ticker", "tickers"))
    if symbol_key is None:
        raise ToolDiscoveryError("historical tool schema has no supported symbol field")
    symbol_spec = properties.get(symbol_key, {})
    arguments: dict[str, Any] = {
        symbol_key: [symbol] if _is_array_schema(symbol_spec) else symbol
    }
    optional = (
        (("interval", "candle_interval"), ("5minute", "5min", "5m", "five_minute")),
        (("span", "range", "time_range"), ("day", "week", "1d", "1w")),
        (("bounds", "session"), ("regular", "regular_hours", "trading")),
    )
    for names, choices in optional:
        key = _first_property(tool, names)
        if key is None:
            continue
        value = _enum_choice(properties.get(key), choices)
        if value is not None:
            arguments[key] = value
    count_key = _first_property(tool, ("limit", "count", "candle_count"))
    if count_key is not None:
        spec = properties.get(count_key, {})
        maximum = spec.get("maximum") if isinstance(spec, Mapping) else None
        arguments[count_key] = min(config.ANALYSIS_CANDLES_TO_RETAIN, int(maximum)) if isinstance(maximum, (int, float)) else config.ANALYSIS_CANDLES_TO_RETAIN
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    for names, value in (
        (("start_time", "starts_at", "from"), current - timedelta(days=7)),
        (("end_time", "ends_at", "to"), current),
    ):
        key = _first_property(tool, names)
        if key is not None and key in _required(tool):
            arguments[key] = value.isoformat().replace("+00:00", "Z")
    unknown = _required(tool) - arguments.keys()
    if unknown:
        raise ToolDiscoveryError("historical tool has unsupported required parameters")
    return arguments


def account_read_arguments(
    tool: McpTool,
    account: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fill only discovered account/time fields; identifiers never leave memory."""

    arguments: dict[str, Any] = {}
    key = _first_property(tool, ("account_number", "account_id", "account"))
    if key is not None:
        account_value = None
        lower = {str(name).lower(): value for name, value in account.items()}
        for candidate in ("account_number", "account_id", "id"):
            if candidate in lower and lower[candidate]:
                account_value = str(lower[candidate])
                break
        if account_value is not None:
            arguments[key] = account_value
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    local = current.astimezone(ZoneInfo(config.MARKET_TIMEZONE))
    local_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    for names, value in (
        (("start_time", "starts_at", "from"), local_start.astimezone(timezone.utc)),
        (("end_time", "ends_at", "to"), current),
    ):
        time_key = _first_property(tool, names)
        if time_key is not None and time_key in _required(tool):
            arguments[time_key] = value.isoformat().replace("+00:00", "Z")
    unknown = _required(tool) - arguments.keys()
    if unknown:
        raise ToolDiscoveryError("account read tool has unsupported required parameters")
    return arguments


def scanner_arguments(
    tool: McpTool,
    *,
    scan_id: str | None,
    scan_name: str,
    account: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    key = _first_property(tool, ("scan_id", "id", "scan_name", "name"))
    if key is None:
        raise ToolDiscoveryError("run_scan schema has no project-scan identifier field")
    value = scan_name if "name" in key.lower() else scan_id
    if not value:
        raise ToolDiscoveryError("saved scanner does not expose the identifier required by run_scan")
    arguments = {key: value}
    if account is not None:
        account_key = _first_property(tool, ("account_number", "account_id", "account"))
        if account_key is not None and account_key != key:
            account_args = account_read_arguments(
                McpTool(tool.name, tool.description, {
                    "type": "object",
                    "properties": {account_key: _properties(tool)[account_key]},
                    "required": [account_key] if account_key in _required(tool) else [],
                }),
                account,
            )
            arguments.update(account_args)
    unknown = _required(tool) - arguments.keys()
    if unknown:
        raise ToolDiscoveryError("scanner tool has unsupported required parameters")
    return arguments


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {str(key): _model_dump(child) for key, child in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_model_dump(child) for child in value]
    return value


def decode_tool_result(result: Any) -> Any:
    """Prefer structured content, falling back to JSON text without logging it."""

    if bool(getattr(result, "is_error", False)):
        raise RobinhoodResponseError("Robinhood MCP tool returned an error")
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return _model_dump(structured)
    content = getattr(result, "content", None)
    if isinstance(content, Sequence):
        texts = [getattr(item, "text", None) for item in content]
        texts = [value for value in texts if isinstance(value, str)]
        if texts:
            joined = "\n".join(texts)
            try:
                return json.loads(joined)
            except json.JSONDecodeError:
                return {"text": joined}
    dumped = _model_dump(result)
    if isinstance(dumped, Mapping):
        return dumped
    raise RobinhoodResponseError("Robinhood MCP tool returned no usable structured data")


class DirectRobinhoodMcpClient:
    """Thread-safe sync facade over one asyncio-owned persistent MCP client."""

    def __init__(
        self,
        *,
        interactive_auth: bool = False,
        storage: KeychainTokenStorage | None = None,
        sdk_factory=None,
        sleep=time.sleep,
    ) -> None:
        self.interactive_auth = interactive_auth
        self.storage = storage or KeychainTokenStorage()
        self.sdk_factory = sdk_factory
        self.sleep = sleep
        self.tools: dict[str, McpTool] = {}
        self.connection_latency_seconds: float | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self._semaphore: asyncio.Semaphore | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._reconnect_event: asyncio.Event | None = None
        self._connection_ready: asyncio.Event | None = None
        self._connection_error: BaseException | None = None

    def __enter__(self) -> "DirectRobinhoodMcpClient":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> "DirectRobinhoodMcpClient":
        if self._thread is not None:
            return self
        if not self.interactive_auth and not self.storage.has_credentials():
            raise RobinhoodAuthenticationRequired(
                "direct Robinhood MCP authorization is required; run python runner.py --robinhood-auth"
            )
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, name="direct-robinhood-mcp", daemon=False)
        self._thread.start()
        startup_wait = 310 if self.interactive_auth else config.ROBINHOOD_MCP_CONNECT_TIMEOUT_SECONDS + 5
        if not self._ready.wait(startup_wait):
            self.close()
            raise DirectMcpUnavailable("direct MCP connection timed out")
        if self._startup_error is not None:
            error = self._startup_error
            self.close()
            if _contains_exception(error, RobinhoodAuthenticationRequired):
                raise RobinhoodAuthenticationRequired(
                    "direct Robinhood MCP authorization is required; run python runner.py --robinhood-auth"
                ) from error
            raise DirectMcpUnavailable("direct MCP connection failed") from error
        return self

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._session_lifecycle())
        except BaseException as exc:
            # This should be unreachable because lifecycle records startup and
            # shutdown failures itself; retain a credential-free final guard.
            if self._startup_error is None:
                self._startup_error = exc
            self._ready.set()
        finally:
            loop.close()

    async def _session_lifecycle(self) -> None:
        """Own SDK enter/exit in one task (required by AnyIO cancel scopes)."""

        self._shutdown_event = asyncio.Event()
        self._reconnect_event = asyncio.Event()
        self._connection_ready = asyncio.Event()
        initial = True
        try:
            while not self._shutdown_event.is_set():
                self._connection_error = None
                started = time.monotonic()
                try:
                    await self._async_start()
                    self.connection_latency_seconds = time.monotonic() - started
                except BaseException as exc:
                    self._connection_error = exc
                    if initial:
                        self._startup_error = exc
                        self._ready.set()
                    self._connection_ready.set()
                    return
                self._connection_ready.set()
                if initial:
                    self._ready.set()
                    initial = False

                shutdown_wait = asyncio.create_task(self._shutdown_event.wait())
                reconnect_wait = asyncio.create_task(self._reconnect_event.wait())
                done, pending = await asyncio.wait(
                    {shutdown_wait, reconnect_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if self._shutdown_event.is_set():
                    return
                if self._reconnect_event.is_set():
                    self._reconnect_event.clear()
                    self._connection_ready.clear()
                    try:
                        await self._async_close()
                    except BaseException as exc:
                        # A broken stream is being discarded before reconnect.
                        # Keep only its exception type in the caller-facing path.
                        self._connection_error = exc
        finally:
            try:
                await self._async_close()
            except BaseException:
                # Robinhood may reject Streamable HTTP session termination with
                # a harmless 400 after a completed request. It must not crash
                # the Python thread or make an already-successful check fail.
                pass

    async def _async_start(self) -> None:
        self._stack = AsyncExitStack()
        callback = None
        if self.interactive_auth:
            callback = LocalOAuthCallback(
                config.ROBINHOOD_MCP_OAUTH_CALLBACK_HOST,
                config.ROBINHOOD_MCP_OAUTH_CALLBACK_PORT,
            )
            callback.start()
            self._stack.callback(callback.close)
        provider = make_oauth_provider(self.storage, interactive=self.interactive_auth, callback=callback)
        if self.sdk_factory is not None:
            context = self.sdk_factory(provider)
        else:
            try:
                import httpx2
                from mcp import Client
                from mcp.client.streamable_http import streamable_http_client
            except ImportError as exc:
                raise DirectMcpUnavailable("official mcp SDK is not installed; install requirements.txt") from exc
            http_client = await self._stack.enter_async_context(
                httpx2.AsyncClient(auth=provider, follow_redirects=True)
            )
            transport = streamable_http_client(
                config.ROBINHOOD_MCP_SERVER_URL,
                http_client=http_client,
                # Robinhood currently rejects the optional Streamable HTTP
                # DELETE termination with 400 after normal completed calls.
                # Closing the owned streams/client is sufficient and avoids a
                # misleading warning on an otherwise clean read-only cycle.
                terminate_on_close=False,
            )
            context = Client(
                transport,
                read_timeout_seconds=config.ROBINHOOD_MCP_REQUEST_TIMEOUT_SECONDS,
                mode="auto",
            )
        self._client = await self._stack.enter_async_context(context)
        self._semaphore = asyncio.Semaphore(config.ROBINHOOD_MCP_MAX_CONCURRENCY)
        listed = await self._client.list_tools(cache_mode="refresh")
        tools = getattr(listed, "tools", [])
        self.tools = {
            str(tool.name): McpTool(
                str(tool.name),
                str(getattr(tool, "description", "") or ""),
                _model_dump(getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}),
            )
            for tool in tools
        }

    async def _async_close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            finally:
                self._stack = None
        self._client = None
        self._semaphore = None

    def close(self) -> None:
        loop, thread = self._loop, self._thread
        if loop is not None and thread is not None and thread.is_alive():
            if self._shutdown_event is not None:
                loop.call_soon_threadsafe(self._shutdown_event.set)
            thread.join(timeout=config.ROBINHOOD_MCP_REQUEST_TIMEOUT_SECONDS + 5)
        self._loop = None
        self._thread = None

    async def _request_reconnect(self) -> None:
        """Ask the lifecycle owner to reconnect; never exit SDK context here."""

        if (
            self._reconnect_event is None
            or self._connection_ready is None
            or self._shutdown_event is None
            or self._shutdown_event.is_set()
        ):
            raise DirectMcpUnavailable("direct MCP session cannot reconnect")
        self._connection_ready.clear()
        self._reconnect_event.set()
        await asyncio.wait_for(
            self._connection_ready.wait(),
            timeout=config.ROBINHOOD_MCP_CONNECT_TIMEOUT_SECONDS,
        )
        if self._connection_error is not None:
            raise DirectMcpUnavailable("direct MCP reconnect failed") from self._connection_error

    def _submit(self, coroutine: Any) -> Any:
        if self._loop is None or self._thread is None or not self._thread.is_alive():
            raise DirectMcpUnavailable("direct MCP client is not connected")
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=config.ROBINHOOD_MCP_REQUEST_TIMEOUT_SECONDS + 5)
        except RobinhoodAuthenticationRequired:
            raise
        except Exception as exc:
            future.cancel()
            text = str(exc).lower()
            if _contains_exception(exc, RobinhoodAuthenticationRequired) or any(
                marker in text for marker in ("401", "unauthorized", "oauth", "authentication")
            ):
                raise RobinhoodAuthenticationRequired(
                    "direct Robinhood MCP authorization is invalid; run python runner.py --robinhood-auth"
                ) from exc
            raise DirectMcpUnavailable("direct MCP request failed") from exc

    def require_tool(self, name: str) -> McpTool:
        if name not in ALLOWED_TOOLS or _unsafe_name(name):
            raise UnsafeToolError("requested MCP operation is outside the safety allowlist")
        try:
            return self.tools[name]
        except KeyError as exc:
            raise ToolDiscoveryError(f"required read-only capability {name} was not discovered") from exc

    async def _call_once(self, name: str, arguments: Mapping[str, Any]) -> ToolCall:
        tool = self.require_tool(name)
        unknown = set(arguments) - set(_properties(tool))
        if unknown:
            raise ToolDiscoveryError("arguments are not present in the discovered tool schema")
        if _required(tool) - set(arguments):
            raise ToolDiscoveryError("required discovered tool arguments are missing")
        if self._client is None or self._semaphore is None:
            raise DirectMcpUnavailable("direct MCP client is not connected")
        started = time.monotonic()
        async with self._semaphore:
            result = await self._client.call_tool(name, dict(arguments))
        return ToolCall(name, dict(arguments), decode_tool_result(result), time.monotonic() - started)

    async def _call_with_retry(self, name: str, arguments: Mapping[str, Any]) -> ToolCall:
        attempts = config.ROBINHOOD_MCP_READ_RETRIES + 1 if name in READ_ONLY_TOOLS else 1
        last: BaseException | None = None
        for attempt in range(attempts):
            try:
                return await asyncio.wait_for(
                    self._call_once(name, arguments),
                    timeout=config.ROBINHOOD_MCP_REQUEST_TIMEOUT_SECONDS,
                )
            except (UnsafeToolError, ToolDiscoveryError, RobinhoodAuthenticationRequired, RobinhoodResponseError):
                raise
            except BaseException as exc:
                last = exc
                if attempt + 1 < attempts:
                    try:
                        await self._request_reconnect()
                    except BaseException as reconnect_error:
                        last = reconnect_error
                    await asyncio.sleep(config.ROBINHOOD_MCP_RETRY_BASE_SECONDS * (2**attempt))
        raise DirectMcpUnavailable("direct MCP read failed after bounded retries") from last

    def call_readonly(self, name: str, arguments: Mapping[str, Any] | None = None) -> ToolCall:
        if name not in READ_ONLY_TOOLS:
            raise UnsafeToolError("only read-only operations are accepted by this method")
        return self._submit(self._call_with_retry(name, arguments or {}))

    def run_project_scan(
        self, *, scan_id: str | None, account: Mapping[str, Any] | None = None
    ) -> ToolCall:
        tool = self.require_tool("run_scan")
        arguments = scanner_arguments(
            tool, scan_id=scan_id, scan_name=config.PROJECT_SCANNER_NAME,
            account=account,
        )
        # Scanner mutations are intentionally never retried automatically.
        return self._submit(self._call_once("run_scan", arguments))

    def get_quotes(self, symbols: Sequence[str]) -> ToolCall:
        normalized = tuple(dict.fromkeys(str(symbol).upper() for symbol in symbols if symbol))
        if not normalized:
            raise ValueError("at least one quote symbol is required")
        tool = self.require_tool("get_equity_quotes")
        key = _first_property(tool, ("symbols", "symbol", "tickers", "ticker"))
        spec = _properties(tool).get(key, {}) if key else {}
        batch_supported = bool(
            key and isinstance(spec, Mapping)
            and _is_array_schema(spec)
        )
        if batch_supported or len(normalized) == 1:
            return self.call_readonly(tool.name, quote_arguments(tool, normalized))

        async def collect_quotes() -> ToolCall:
            started = time.monotonic()
            calls = await asyncio.gather(
                *(self._call_with_retry(tool.name, quote_arguments(tool, (symbol,))) for symbol in normalized)
            )
            return ToolCall(
                tool.name,
                {"batch_mode": "bounded_individual_calls", "symbol_count": len(normalized)},
                {"results": [call.value for call in calls]},
                time.monotonic() - started,
            )

        return self._submit(collect_quotes())

    def get_historicals(self, symbol: str) -> ToolCall:
        normalized = str(symbol).upper().strip()
        tool = self.require_tool("get_equity_historicals")
        return self.call_readonly(tool.name, historical_arguments(tool, normalized))

    def get_historicals_many(self, symbols: Sequence[str]) -> list[ToolCall | BaseException]:
        normalized = tuple(dict.fromkeys(str(symbol).upper() for symbol in symbols if symbol))

        async def collect() -> list[ToolCall | BaseException]:
            tool = self.require_tool("get_equity_historicals")
            # Do not let a long universe history batch fill the global MCP
            # semaphore. Quotes use the remaining capacity and can interleave
            # instead of waiting behind every queued historical request.
            limiter = asyncio.Semaphore(
                max(1, min(
                    config.ROBINHOOD_MCP_HISTORICAL_BATCH_CONCURRENCY,
                    config.ROBINHOOD_MCP_MAX_CONCURRENCY - 1,
                ))
            )

            async def historical(symbol):
                async with limiter:
                    return await self._call_with_retry(
                        tool.name, historical_arguments(tool, symbol)
                    )

            tasks = [historical(symbol) for symbol in normalized]
            return list(await asyncio.gather(*tasks, return_exceptions=True))

        return self._submit(collect())

    def tool_inventory(self) -> tuple[McpTool, ...]:
        return tuple(sorted(self.tools.values(), key=lambda item: item.name))
