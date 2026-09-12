"""Independent OAuth/PKCE setup with macOS Keychain persistence.

No Codex files, browser cookies, or Robinhood application credentials are read.
Only the official MCP SDK receives token values, and values are never logged.
"""

from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

import config
from robinhood_mcp.errors import DirectMcpUnavailable, RobinhoodAuthenticationRequired

KEYCHAIN_SERVICE = "robinhood-ai-trader.direct-mcp"
TOKEN_ENTRY = "oauth-token"
CLIENT_ENTRY = "oauth-client-registration"


class KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def delete_password(self, service_name: str, username: str) -> None: ...


class KeychainTokenStorage:
    """MCP TokenStorage backed by the current OS keyring (Keychain on macOS)."""

    def __init__(self, backend: KeyringBackend | None = None) -> None:
        if backend is None:
            try:
                import keyring
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise DirectMcpUnavailable("keyring package is not installed") from exc
            backend = keyring
        self.backend = backend

    @staticmethod
    def _model(model_name: str, raw: str | None) -> Any:
        if raw is None:
            return None
        try:
            from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise DirectMcpUnavailable("official mcp package is not installed") from exc
        model = OAuthToken if model_name == "token" else OAuthClientInformationFull
        return model.model_validate_json(raw)

    async def get_tokens(self) -> Any:
        return self._model("token", self.backend.get_password(KEYCHAIN_SERVICE, TOKEN_ENTRY))

    async def set_tokens(self, tokens: Any) -> None:
        self.backend.set_password(KEYCHAIN_SERVICE, TOKEN_ENTRY, tokens.model_dump_json())

    async def get_client_info(self) -> Any:
        return self._model("client", self.backend.get_password(KEYCHAIN_SERVICE, CLIENT_ENTRY))

    async def set_client_info(self, client_info: Any) -> None:
        self.backend.set_password(KEYCHAIN_SERVICE, CLIENT_ENTRY, client_info.model_dump_json())

    def has_credentials(self) -> bool:
        """Return only presence; never deserialize or expose token text."""
        return bool(
            self.backend.get_password(KEYCHAIN_SERVICE, TOKEN_ENTRY)
            and self.backend.get_password(KEYCHAIN_SERVICE, CLIENT_ENTRY)
        )


class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "RobinhoodDirectMcpCallback"

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        self.server.callback_params = parse_qs(parsed.query)  # type: ignore[attr-defined]
        body = b"Robinhood MCP authorization received. You may close this window."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.server.callback_event.set()  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        # Query parameters include the authorization code and must never be logged.
        return


class LocalOAuthCallback:
    def __init__(self, host: str, port: int, *, timeout: float = 300) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}/callback"

    def start(self) -> None:
        server = ThreadingHTTPServer((self.host, self.port), _CallbackHandler)
        server.callback_event = threading.Event()  # type: ignore[attr-defined]
        server.callback_params = None  # type: ignore[attr-defined]
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="robinhood-oauth-callback", daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.server = None
        self.thread = None

    def wait(self) -> dict[str, list[str]]:
        if self.server is None:
            raise RuntimeError("callback server is not running")
        if not self.server.callback_event.wait(self.timeout):  # type: ignore[attr-defined]
            raise RobinhoodAuthenticationRequired("OAuth browser callback timed out")
        params = self.server.callback_params  # type: ignore[attr-defined]
        if not isinstance(params, dict) or "code" not in params:
            raise RobinhoodAuthenticationRequired("OAuth callback did not contain an authorization code")
        return params


def make_oauth_provider(
    storage: KeychainTokenStorage,
    *,
    interactive: bool,
    callback: LocalOAuthCallback | None = None,
    browser_open=webbrowser.open,
) -> Any:
    """Build the official SDK provider; it owns discovery, PKCE and refresh."""

    try:
        from pydantic import AnyUrl
        from mcp.client.auth import OAuthClientProvider
        from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise DirectMcpUnavailable("official mcp SDK dependencies are not installed") from exc

    redirect_uri = (
        callback.redirect_uri
        if callback is not None
        else f"http://{config.ROBINHOOD_MCP_OAUTH_CALLBACK_HOST}:{config.ROBINHOOD_MCP_OAUTH_CALLBACK_PORT}/callback"
    )

    async def redirect_handler(auth_url: str) -> None:
        if not interactive:
            raise RobinhoodAuthenticationRequired(
                "direct Robinhood MCP authorization is required; run python runner.py --robinhood-auth"
            )
        print("OPENING ROBINHOOD AUTHORIZATION IN YOUR BROWSER", flush=True)
        if not browser_open(auth_url):
            print(f"AUTHORIZATION URL: {auth_url}")

    async def callback_handler() -> Any:
        if not interactive or callback is None:
            raise RobinhoodAuthenticationRequired("interactive OAuth callback is unavailable")
        params = await asyncio.to_thread(callback.wait)
        return AuthorizationCodeResult(
            code=params["code"][0],
            state=params.get("state", [None])[0],
            iss=params.get("iss", [None])[0],
        )

    return OAuthClientProvider(
        server_url=config.ROBINHOOD_MCP_SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Robinhood AI Trader Direct MCP",
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="internal",
        ),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
