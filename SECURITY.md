# Security Policy

As a U.S. Government agency, the General Services Administration (GSA) takes seriously our responsibility to protect the public's information, including financial and personal information, from unwarranted disclosure.

Software developed by the U.S. General Services Administration (GSA) is subject to the GSA Vulnerability Disclosure Policy.

Please consult our policy for:

How to submit a report if you believe you have discovered a vulnerability.
GSA's coordinated disclosure policy.
Information on how you may conduct security research on GSA developed software and systems.
Important legal and policy guidelines.

## Supported Versions

| Version (Branch) | Supported          |
| ------- | ------------------ |
| main   | :white_check_mark: |
| other  | :x:                |

## Security Researchers

Security researchers shall:

- Make every effort to avoid privacy violations, degradation of user experience, disruption to production systems, and destruction or manipulation of data.
- Only use exploits to the extent necessary to confirm a vulnerability. Do not use an exploit to compromise or exfiltrate data, establish command line access and/or persistence, or use the exploit to "pivot" to other systems. Once you've established that a vulnerability exists, or encountered any of the sensitive data outlined above, you must stop your test and notify us immediately.
- Keep confidential any information about discovered vulnerabilities for up to 90 calendar days after you have notified GSA. For details, please review Coordinated Disclosure.

## Security posture of this server

This is a **hackathon prototype over synthetic data**, not a deployed system. Its
posture is summarized below; see [`docs/project/SECURITY-NOTES.md`](docs/project/SECURITY-NOTES.md)
for the detailed basis.

**Read / view-only by construction.** No tool edits, writes, or deletes the model.
The tool surface splits into *resolve* (find which elements a request is about) and
*act* (highlight, isolate, move the camera, reset). Every act maps to a viewer
command on an explicit allow-list; there is no write, purchase, or delete path.

**Synthetic data only — no PII or CUI.** The server operates over one synthetic
mechanical (MECH) building model with synthetic, Maximo-style asset and cost data.
It is **not** connected to a real Maximo system or any live facility data. The
model, queries, dataset, and traces contain no personal or controlled information.

**Validated inputs.** Tool parameters are Pydantic-typed with `Literal`
allow-lists for categories and levels — the model cannot pass a raw `asset_type`,
free-form field, or SQL. Element ids are shape-validated against the durable Revit
UniqueId format, with length caps; anything that fails validation is dropped.

**Fixed upstream origin (SSRF guard).** The viewer backend origin is fixed by
server configuration (`VIEWER_BASE_URL`), never taken from a tool argument. The
HTTP helper rejects absolute or escaping paths, disables redirects and environment
proxies, and sanitizes upstream failures. Do not replace its relative path with a
tool-supplied URL. If a tool must ever fetch arbitrary URLs, require exact
destination allow-lists, connection-time validation of every resolved IPv4/IPv6
address, validation at every redirect hop, credential stripping across origins, and
network-level egress controls. A DNS lookup followed by a normal hostname request
is vulnerable to DNS rebinding.

**No unauthenticated secrets, minimal logs.** The MCP server needs no credentials
of its own. Eval and demo credentials load from a `.env` outside the repo (via
`GSA_AI_ENV_PATH`); the Databricks token for the demo backend is minted fresh at
startup and never committed. Logs use structured, explicitly allow-listed fields —
no upstream bodies, ids, URLs, query strings, stack traces, or PII.

**Open `/mcp` endpoint (hackathon posture).** When run over HTTP, the `/mcp`
endpoint is exposed **without authentication** by default, matching the
public-data pilot posture. This is acceptable for a hackathon over synthetic data.
Before any non-hackathon use, front the endpoint with authentication and review
your agency's ATO requirements.

**Not yet covered.** Prompt-injection and adversarial-input testing, and a stricter
data classification, would be required before any production use over real facility
data (which could expose building or security information).

Treat all tool arguments, retrieved content, and agent-to-agent messages as
untrusted. Do not include upstream response bodies, headers, full URLs, query
strings, stack traces, credentials, names, SSNs, dates of birth, addresses, or other
sensitive values in MCP errors or logs. Use structured logs with explicitly
allowlisted fields and define retention and access controls before processing PII.
