# Plan / state of the work

- `craft-conn.py` is verified against Mark's real space (REWILD): `--list`, `--tree`,
  `--doc`, auth, and error paths, and now also a full Gemini call with a valid key
  (`--list-models` and a live completion both succeeded 2026-09-15).
- `design.py` ran end-to-end against the real space and a valid Gemini key on
  2026-09-15: draft + folder load, streaming reply, `/context`, a follow-up turn, and
  session save all worked. `/add` also verified (2026-09-15): resolves a partial
  title, loads it into the working set, and tells the model in-band. `/drop` and
  `/craft` are still unexercised. `/craft` writes to the real Craft space — it only
  ever creates new documents, never overwrites, and confirms y/N first. Keep it that
  way.
- The MCP path in `craft-conn.py` works against a local MCP server but has never
  completed Craft's real OAuth handshake.
- The `/folders` vs `/documents` subtraction in `documents_in()` is now verified
  against real nesting (2026-09-15) — see [notes.md](notes.md).
- `tests/` holds fake Craft servers covering each shape. Use them instead of the live
  API when changing the client.

## Next up

- Exercise `/drop` and `/craft` in `design.py` (the latter creates a real document in
  Mark's Craft space — confirm before running).
- Run the MCP path against Craft's real OAuth handshake, not just the local fake
  server. Mark's real MCP endpoint for this: `https://mcp.craft.do/links/CG8L3Bsjeb0/mcp`.
  This needs no stored credential — running with no `--token`/`CRAFT_API_TOKEN` set
  opens a browser to Craft's real consent screen, which Mark completes himself.
