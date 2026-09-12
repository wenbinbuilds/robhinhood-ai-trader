# Direct Robinhood MCP implementation

The production factual provider is now `DirectRobinhoodMcpClient`, built on the
official `mcp==2.2.0` Python SDK and Streamable HTTP endpoint:

`https://agent.robinhood.com/mcp/trading`

The SDK's `OAuthClientProvider` owns protected-resource and authorization-server
discovery, dynamic client registration, authorization-code flow, PKCE,
state/issuer validation, and refresh. This application supplies a localhost
callback and a `TokenStorage` implementation backed by macOS Keychain.

Manual browser consent is intentionally required once:

```bash
python runner.py --robinhood-auth
```

Normal `--once` and `--loop` runs are noninteractive. If this application's own
grant is absent or invalid, they fail clearly and tell the operator to run that
command. They never copy Codex OAuth data, inspect Codex files, read cookies, or
silently fall back to `codex exec`.

After authorization:

```bash
python runner.py --robinhood-mcp-check
python runner.py --once
```

The first command discovers actual tool input schemas and performs only an
Agentic-account read and connectivity quote. The second builds the factual
snapshot directly, then runs the existing single reasoning pass and local
deterministic pipeline.

Sources:

- [Robinhood Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
- [Official MCP Python SDK OAuth client guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/oauth-clients.md)
- [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
