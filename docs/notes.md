# Notes

Traps and non-obvious behavior, recorded so they aren't rediscovered the hard way.

## Craft API traps

**The API base 307-redirects to Craft's PUBLIC docs viewer.** `requests` follows
redirects by default, so a bare `GET` on the base returns HTTP 200 with ~2.8MB of
OpenAPI documentation instead of an auth error. This silently fed API docs to the
model once already. `CraftREST._send` now raises when a request lands on a `/docs/`
path it did not ask for — do not remove that guard.

**Endpoint shape:** `https://connect.craft.do/links/<id>/api/v1`. Content is at
`GET /blocks?id=<documentId>`, *not* at the base. `Accept: text/markdown` makes Craft
render markdown server-side — much better than walking block JSON, which is only the
fallback for connections that ignore the header.

**`/documents` is flat; `/folders` is the hierarchy.** Listing documents without a
filter returns everything with no structure. `documents_in()` includes subfolders, so
the tree walk subtracts what child folders already claimed to avoid double-counting.
Verified against real nesting on 2026-09-15 (Rulebook/Test, one document each): `--list`
attributed each document to its own folder with no double-counting.

**A "folder" in Craft's sidebar is often a document with sub-pages.** `GET /blocks`
defaults to `maxDepth=-1`, so `--doc` already pulls the whole subtree. `--tree` shows
that structure. `resolve_folder` detects this case and redirects the user to `--doc`.

**Rate limits:** 50 req/10s per IP, 100/60s per space, shared across API links and MCP
connections. Requests are spaced ~150ms; 429 is retried once per `Retry-After`.

## Python environment

- The PyPI package named **`google`** is an unrelated search scraper. The SDK is
  **`google-genai`** (`from google import genai`). This wasted a debugging cycle.
- `mcp` **2.x renamed model fields to snake_case** (`input_schema`, `is_error`,
  `structured_content`); 1.x used camelCase. The code reads both. When it read only
  camelCase, tool arguments silently went out as `{}` and server-side errors came back
  looking like document content — wrong answers that looked right.
- `mcp` 2.x also renamed `streamablehttp_client` → `streamable_http_client` and
  `FastMCP` → `MCPServer`.
- anyio wraps errors in `ExceptionGroup`, so `main()` unwraps before reporting.
- Both scripts re-exec into `.venv` when the interpreter is too old **or** missing
  dependencies — `#!/usr/bin/env python3` finds 3.14 without packages on this machine.

## Gemini

- Model ids churn fast; 2.5 was retired mid-2026. `--list-models` reads the live list.
  Default is currently `gemini-3.6-flash` (Mark set this); 3.8 shipped 2026-09-02.
- Flash is the free tier. **Pro is paid-only** — it will appear in `--list-models` and
  still reject at request time until billing is enabled.
- There is **no official API to push content into the Gemini macOS app** or a Gem.
  Only reverse-engineered cookie-driven libraries, which are not worth it. The
  supported bridge is `--dry-run --out file.md`, then attach the file in the app.
- `--project` means a **Google Cloud project via Vertex AI** (needs ADC, not an API
  key). It is unrelated to the Gemini app's notion of projects.
