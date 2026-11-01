# Security Notes — Role B (viewer command channel)

This project follows the hackathon security posture described in the template
`SECURITY.md`: an MCP / command endpoint may be **unauthenticated** when it
serves **public pilot data**, provided the always-on hygiene controls below are
enforced. Before any non-hackathon use, the endpoint must be fronted with
authentication and reviewed against the agency's ATO requirements.

## Current posture (hackathon)

- **Data:** public pilot data only (the CA8888 MECH model + synthetic Maximo
  records). No PII.
- **`/ws/viewer` WebSocket command channel:** unauthenticated, **same-origin**.
  The browser connects only to its own origin (`ws(s)://<this-host>/ws/viewer`).
  This matches the template's "unauthenticated over public data" allowance.

## Always-on controls enforced now

- **No secrets in source.** Tokens come from environment / the Databricks CLI,
  never committed. The command channel carries no credentials.
- **All inbound data is untrusted.** The browser dispatcher (`runCommand`)
  accepts only a fixed command allowlist
  (`highlight_elements`, `highlight_rooms`, `isolate`, `zoom_to`, `color_by`,
  `reset`). Every argument is validated:
  - element / room identifiers must match the Revit UniqueId shape
    (`GUID-8hex`); anything else is dropped;
  - arrays are length-capped (`MAX_IDS`) to prevent flooding.
  Unknown or malformed commands are ignored (fail-safe), never executed.
- **No code execution, navigation, fetch, or path access.** Commands carry only
  identifiers / field names — never URLs or file paths. The channel cannot eval
  code, navigate the page, or trigger outbound requests. No DNS-rebinding
  surface (no tool-supplied URLs, no redirects, no proxies).
- **Sanitized errors and logs.** Command handlers return small, allowlisted
  summaries (e.g. `{applied, count}`). Server logs use explicitly allowlisted
  fields only (connection counts, command name, client count) and never include
  upstream bodies, identifiers, URLs, query strings, stack traces, credentials,
  or any PII. Client-facing errors are generic (e.g. "Genie request failed").
- **Backend broadcast is also allowlisted.** `ViewerHub.broadcast` refuses to
  send anything outside `ALLOWED_COMMANDS`.

## Pre-production gates (NOT built — required before non-hackathon use)

- **Authentication / SSO (OIDC).** Front the App and the `/ws/viewer` + `/mcp`
  endpoints with workspace SSO or an identity-aware proxy.
- **Per-user authorization** and **audit logging** of commands.
- **Network egress controls** and connection-time destination validation if any
  tool is ever allowed to fetch external resources.
- **PII handling.** Define retention and access controls before processing any
  non-public data; keep structured logs with allowlisted fields only.
- **ATO review** per agency policy.

## Known limitations (functional, not security)

- `highlight_rooms` / `zoom_to`-by-room over the command channel require room
  geometry (`wkt`), which the ID-only broadcast does not currently carry. The
  chat flow highlights rooms via the HTTP `/api/chat` response (which includes
  geometry); a pure MCP-driven room highlight would need the server to fetch and
  include room geometry in the command. `color_by` is a **planned future tool**
  (not registered by the MCP server); the demo viewer's command handler accepts
  it defensively and returns `not_implemented` (fails safe).
