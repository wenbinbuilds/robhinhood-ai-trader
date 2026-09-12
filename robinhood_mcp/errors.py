"""Credential-safe direct MCP exception taxonomy."""


class DirectMcpError(RuntimeError):
    """Base class for direct MCP failures safe to report by type."""


class DirectMcpUnavailable(DirectMcpError):
    """The SDK, transport, or remote MCP service is unavailable."""


class RobinhoodAuthenticationRequired(DirectMcpError):
    """This application's independent Robinhood OAuth grant is absent/expired."""


class RobinhoodResponseError(DirectMcpError):
    """Robinhood returned malformed or incomplete factual data."""


class ToolDiscoveryError(DirectMcpError):
    """A required capability was not present in the discovered tool inventory."""


class UnsafeToolError(DirectMcpError):
    """A caller attempted to invoke a tool outside the hard safety allowlist."""
