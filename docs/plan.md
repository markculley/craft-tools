# Plan / state of the work

- `craft-conn.py` is verified against Mark's real space (REWILD): `--list`, `--tree`,
  `--doc`, auth, and error paths, and now also a full Gemini call with a valid key
  (`--list-models` and a live completion both succeeded 2026-09-15).
- `design.py` ran end-to-end against the real space and a valid Gemini key on
  2026-09-15: draft + folder load, streaming reply, `/context`, a follow-up turn, and
  session save all worked. `/add`, `/drop`, and `/craft` are all verified (2026-09-15):
  `/add` resolves a partial title and tells the model in-band; `/drop` removes it from
  the working set; `/craft` synthesised the session and created a real document in
  Mark's Craft space (confirms y/N first, only ever creates — never overwrites).
- A mid-session Gemini error (a transient 503 surfaced this) used to kill the whole
  REPL and skip the session save, per `docs/notes.md`. Fixed 2026-09-15: each
  `stream_reply()` call in the loop is now caught individually, so one failed turn no
  longer loses the rest of the session.
- The MCP path in `craft-conn.py` works against a local MCP server but has never
  completed Craft's real OAuth handshake.
- The `/folders` vs `/documents` subtraction in `documents_in()` is now verified
  against real nesting (2026-09-15) — see [notes.md](notes.md).
- `tests/` holds fake Craft servers covering each shape. Use them instead of the live
  API when changing the client.

## Next up

- Run the MCP path against Craft's real OAuth handshake, not just the local fake
  server. Mark's real MCP endpoint for this: `https://mcp.craft.do/links/CG8L3Bsjeb0/mcp`.
  This needs no stored credential — running with no `--token`/`CRAFT_API_TOKEN` set
  opens a browser to Craft's real consent screen, which Mark completes himself.
