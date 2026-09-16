# craft-tools

Two tools for working on [Craft](https://www.craft.do) documents with Google Gemini.

- **`craft-conn.py`** — one-shot: pull a document out of Craft, send it to Gemini, print the answer.
- **`design.py`** — interactive: hold a design conversation about a draft, with research documents standing in context.

Both talk to a Craft **API connection** (Connections tab in the Craft sidebar), and
`craft-conn.py` also supports Craft's OAuth **MCP** endpoint.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

export CRAFT_API_ENDPOINT="https://connect.craft.do/links/<id>/api/v1"
export CRAFT_API_TOKEN="<token from the Connections tab>"
export GEMINI_API_KEY="<https://aistudio.google.com/apikey>"
```

Both scripts re-exec into `.venv` if launched under an older Python, so `./craft-conn.py`
works even when `python3` on PATH is the system 3.9.

## craft-conn.py

```bash
./craft-conn.py --list                    # folders and documents
./craft-conn.py --tree                    # documents with their nested sub-pages
./craft-conn.py --doc "Rulebook v0.1"     # one document -> Gemini
./craft-conn.py --folder "Research"       # a whole folder as one corpus
./craft-conn.py --search "tension"        # everything matching
./craft-conn.py --list-models             # model ids this key can use
```

`--dry-run` fetches from Craft and prints it without spending a Gemini call — use it
to check what the model would actually see. `--out FILE` writes the result instead of
printing it. `--prompt` / `--prompt-file` replace the default instruction.

Sub-pages are included automatically: `GET /blocks` defaults to `maxDepth=-1`, so a
document that looks like a folder in Craft's sidebar comes through whole.

## design.py

```bash
./design.py --draft "Rulebook v0.1" --context Research
```

The draft is treated as mutable; `--context` documents and folders are background the
model reasons from but does not propose rewriting. In-session:

```
/add <name>      load another document or folder mid-conversation
/drop <name>     remove one
/context         what is loaded, and roughly how many tokens
/craft [title]   synthesise the decisions and write them back to Craft
/save [path]     transcript to a file
/quit
```

`/craft` always creates a **new** document — it never overwrites.

## Notes

- Craft's rate limits are 50 requests/10s per IP and 100/60s per space, shared across
  API links and MCP connections. Requests are spaced ~150ms and a 429 is retried once.
- The API base URL 307-redirects to Craft's *public* API documentation. Requesting it
  directly returns the spec, not your content; both tools detect this and error rather
  than pass documentation downstream as if it were a document.
- Vertex AI is supported via `--project <gcp-project>` instead of an API key.

## Prior art

[`pa1ar/craft-cli`](https://github.com/pa1ar/craft-cli) covers the same REST API far more
completely (tasks, collections, backlinks, undo/diff) in TypeScript, and reads Craft's
local SQLite cache to avoid rate limits. Worth using if you want general Craft scripting;
these tools exist for the Gemini design-session workflow, which it does not cover.
