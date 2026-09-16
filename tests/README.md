# Test fixtures

Fake Craft servers implementing the documented `/api/v1` contract. Each runs
standalone; point a tool at it with `--endpoint http://127.0.0.1:<port>/links/TEST/api/v1`
and `--token tok`.

| Fixture | Shape it reproduces |
|---|---|
| `fake_craft_flat.py` | documents with no folders; markdown + JSON block responses |
| `fake_craft_folders.py` | nested folders; exercises document-to-deepest-folder attribution |
| `fake_craft_subpages.py` | REWILD's real shape: built-in locations only, docs with nested sub-pages |
| `fake_craft_auth.py` | accepts exactly ONE auth header style, named in argv — tests the fallback |
| `fake_craft_redirect.py` | 307s the API base to a public docs page, as Craft really does |
| `fake_mcp.py` | MCP server over streamable HTTP (`mcp` arg) and a plain REST server (`rest`) |

```bash
.venv/bin/python tests/fake_craft_subpages.py 8820 &
CRAFT_API_TOKEN=tok ./craft-conn.py --endpoint http://127.0.0.1:8820/links/TEST/api/v1 --tree
```

`fake_craft_auth.py` takes a scheme as its second argument:
`bearer`, `x-api-key`, `raw-authorization`, `api-key`, or `never`.
