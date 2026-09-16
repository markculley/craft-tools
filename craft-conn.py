#!/usr/bin/env python3
"""Bridge Craft documents into Google Gemini.

Pulls document content from a Craft connection -- either the REST "API
connection" or the OAuth-protected MCP endpoint -- and sends it to Gemini for
analysis.

Quick start:

    export CRAFT_API_ENDPOINT="https://connect.craft.do/links/<id>/api/v1"
    export GEMINI_API_KEY="..."            # or use --project for Vertex AI
    ./craft-conn.py

Craft gives you the endpoint (and, for REST connections, credentials) under
Connections in the sidebar.  Both connection kinds are supported; the mode is
detected from the URL unless you pass --mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import re
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# This file lives next to iTerm2's managed venv, but `#!/usr/bin/env python3`
# finds whatever python3 is first on PATH -- often the system 3.9, which is too
# old for our dependencies (and for BaseExceptionGroup). The venv path contains
# a space, so a shebang cannot point at it directly; re-exec instead.
def _find_venv_python() -> pathlib.Path | None:
    """Locate the project venv.

    It sits beside the script in a normal checkout, but one level up in the
    iTerm2 Scripts layout, so check both rather than assuming either.
    """
    here = pathlib.Path(__file__).resolve().parent
    for base in (here, here.parent):
        candidate = base / ".venv" / "bin" / "python"
        if candidate.exists():
            return candidate
    return None


def _wrong_interpreter() -> str:
    """Why the current interpreter cannot run this, or '' if it can.

    `#!/usr/bin/env python3` picks up whatever is first on PATH, which may be
    too old OR merely be missing our dependencies. Both need the venv.
    """
    if sys.version_info < (3, 11):
        return f"Python 3.11+ required, but this is {sys.version.split()[0]}"
    try:
        import requests  # noqa: F401
    except ImportError:
        return f"dependencies missing from {sys.executable}"
    return ""


_PROBLEM = _wrong_interpreter()
if _PROBLEM:
    _VENV_PYTHON = _find_venv_python()
    if _VENV_PYTHON and not os.environ.get("CRAFT_CONN_NO_REEXEC"):
        os.environ["CRAFT_CONN_NO_REEXEC"] = "1"
        os.execv(
            str(_VENV_PYTHON),
            [str(_VENV_PYTHON), str(pathlib.Path(__file__).resolve()), *sys.argv[1:]],
        )
    sys.exit(
        f"{pathlib.Path(__file__).name}: {_PROBLEM}.\n"
        "Create the project venv first:\n"
        "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    )

import requests

try:
    import iterm2
except ModuleNotFoundError:
    iterm2 = None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_LOCATION = "global"
OAUTH_CALLBACK_PORT = 33418
CONFIG_DIR = Path(os.environ.get("CRAFT_CONN_HOME", Path.home() / ".craft-conn"))

DEFAULT_PROMPT = """\
You are an expert tabletop game designer analyzing a rulebook draft.
Review the following rules text for clarity, edge-case ambiguities, and
structural flow. Be specific and cite the passages you are reacting to.
"""

# Craft does not publish stable MCP tool names, so we match on intent and let
# --tool override when the heuristics guess wrong.
READ_TOOL_HINTS = ("get_document", "read_document", "fetch_document", "get_doc", "read")
LIST_TOOL_HINTS = ("list_documents", "list_docs", "search_documents", "search", "list")


class CraftConnError(RuntimeError):
    """Anything that goes wrong talking to Craft or Gemini."""


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

TEXT_KEYS = ("title", "name", "markdown", "content", "text", "body", "value")


def extract_text(payload: Any, _depth: int = 0) -> str:
    """Flatten an arbitrary Craft JSON payload into readable text.

    Craft's response shape differs between connection types and API versions,
    so rather than binding to one schema we walk the structure and keep the
    string fields that plausibly carry document content.
    """
    if _depth > 12:
        return ""
    if payload is None or isinstance(payload, bool):
        return ""
    if isinstance(payload, (str, int, float)):
        return str(payload).strip()
    if isinstance(payload, list):
        parts = [extract_text(item, _depth + 1) for item in payload]
        return "\n".join(p for p in parts if p)

    if isinstance(payload, dict):
        preferred = [
            extract_text(payload[key], _depth + 1) for key in TEXT_KEYS if key in payload
        ]
        preferred = [p for p in preferred if p]
        if preferred:
            return "\n".join(preferred)
        # No recognised key at this level: keep descending.
        parts = [extract_text(value, _depth + 1) for value in payload.values()]
        return "\n".join(p for p in parts if p)

    return ""


def _looks_like_mcp(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname == "mcp.craft.do" or parsed.path.rstrip("/").endswith("/mcp")


# --------------------------------------------------------------------------
# Craft: REST connection
# --------------------------------------------------------------------------

# The endpoint Craft hands out is an API *base* (".../api/v1"); a bare GET on it
# returns the OpenAPI spec, not your content. Real content comes from
# GET /blocks?id=<documentId>, which renders markdown directly when asked.
ACCEPT_JSON = "application/json"
ACCEPT_MARKDOWN = "text/markdown"

# Craft documents Bearer, but connections have varied; try the common spellings.
AUTH_STYLES = (
    ("bearer", lambda t: {"Authorization": f"Bearer {t}"}),
    ("x-api-key", lambda t: {"X-API-Key": t}),
    ("raw-authorization", lambda t: {"Authorization": t}),
    ("api-key", lambda t: {"Api-Key": t}),
)

# Craft's built-in locations are filtered by name; real folders by id.
BUILTIN_LOCATIONS = {"unsorted", "trash", "templates", "daily_notes"}

# Space limit is 100 requests/60s; stay well under it when walking many docs.
REQUEST_SPACING = 0.15


class CraftREST:
    """Thin client for a Craft API-connection base URL."""

    def __init__(self, base: str, token: str | None, extra_headers: dict[str, str] | None):
        self.base = base.rstrip("/")
        self.token = token
        self.extra = extra_headers or {}
        self.session = requests.Session()
        self._auth: dict[str, str] | None = None
        self._last_request = 0.0

    # -- transport ---------------------------------------------------------

    def _auth_candidates(self) -> list[tuple[str, dict[str, str]]]:
        if self.extra:
            return [("custom", dict(self.extra))]
        if self.token:
            return [(name, build(self.token)) for name, build in AUTH_STYLES]
        return [("none", {})]

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < REQUEST_SPACING:
            time.sleep(REQUEST_SPACING - elapsed)
        self._last_request = time.monotonic()

    def _send(self, path: str, params: dict[str, Any], accept: str, auth: dict[str, str]):
        self._throttle()
        headers = {"Accept": accept, **auth}
        url = f"{self.base}{path}"
        try:
            response = self.session.get(url, headers=headers, params=params, timeout=60)
        except requests.RequestException as exc:
            raise CraftConnError(f"Unable to reach Craft at {url}: {exc}") from exc

        # The API base 307-redirects to a PUBLIC docs viewer. Following it yields
        # a cheerful 200 holding the API reference instead of your content --
        # exactly the kind of wrong answer that looks like a right one.
        if "/docs/" in urlparse(response.url).path and "/docs/" not in urlparse(url).path:
            raise CraftConnError(
                f"{url} redirected to Craft's public API documentation "
                f"({response.url}), which means no document was fetched. "
                "Point --endpoint at the API base ending in /api/v1 and make sure "
                "a token is set, rather than requesting the base URL itself."
            )
        return response

    def get(self, path: str, params: dict[str, Any] | None = None, accept: str = ACCEPT_JSON):
        params = params or {}

        if self._auth is None:
            # First call: discover which auth header this connection accepts.
            last = None
            for name, auth in self._auth_candidates():
                response = self._send(path, params, accept, auth)
                if response.ok:
                    if name not in ("bearer", "custom", "none"):
                        print(f"Authenticated with the {name} header.", file=sys.stderr)
                    self._auth = auth
                    return response
                last = response
                if response.status_code not in (401, 403):
                    break
            raise CraftConnError(self._describe(last, path))

        response = self._send(path, params, accept, self._auth)

        if response.status_code == 429:
            delay = float(response.headers.get("Retry-After", 5))
            print(f"Rate limited by Craft; retrying in {delay:.0f}s.", file=sys.stderr)
            time.sleep(delay)
            response = self._send(path, params, accept, self._auth)

        if not response.ok:
            raise CraftConnError(self._describe(response, path))
        return response

    def _describe(self, response: requests.Response | None, path: str) -> str:
        if response is None:
            return f"Craft request to {path} failed."
        status = response.status_code
        if status in (401, 403):
            hint = (
                " Check the token from Craft's Connections tab, or pass the exact header "
                "with --header 'Name: value'."
            )
        elif status == 404:
            hint = " Check that --endpoint is the API base URL ending in /api/v1."
        else:
            hint = ""
        body = (response.text or "").strip().replace("\n", " ")
        snippet = f" Response: {body[:200]}" if body else ""
        return f"Craft returned HTTP {status} for {path}.{hint}{snippet}"

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.get(path, params, ACCEPT_JSON)
        try:
            return response.json()
        except ValueError as exc:
            raise CraftConnError(f"Craft sent non-JSON for {path}: {exc}") from exc

    # -- API surface -------------------------------------------------------

    def connection_info(self) -> dict[str, Any]:
        return self.get_json("/connection")

    def list_documents(self, location: str | None = None) -> list[dict[str, Any]]:
        params = {"location": location} if location else {}
        payload = self.get_json("/documents", params)
        return payload.get("items", []) if isinstance(payload, dict) else []

    def list_folders(self) -> list[dict[str, Any]]:
        payload = self.get_json("/folders")
        return payload.get("items", []) if isinstance(payload, dict) else []

    def documents_in(self, node_id: str) -> list[dict[str, Any]]:
        """Documents in a location, including its subfolders.

        Built-in locations filter by name; user folders filter by id.
        """
        key = "location" if node_id in BUILTIN_LOCATIONS else "folderId"
        payload = self.get_json("/documents", {key: node_id})
        return payload.get("items", []) if isinstance(payload, dict) else []

    def document_markdown(self, document_id: str) -> str:
        """Fetch a document's rendered content.

        Craft renders markdown server-side when asked, which beats walking the
        block tree ourselves.
        """
        response = self.get("/blocks", {"id": document_id}, ACCEPT_MARKDOWN)
        text = (response.text or "").strip()
        if text and not text.lstrip().startswith("{"):
            return tidy_craft_markdown(text)
        # Some connections ignore the Accept header and return JSON blocks.
        return tidy_craft_markdown(
            blocks_to_markdown(self.get_json("/blocks", {"id": document_id}))
        )

    def blocks_json(self, document_id: str, max_depth: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"id": document_id}
        if max_depth is not None:
            params["maxDepth"] = max_depth
        payload = self.get_json("/blocks", params)
        return payload if isinstance(payload, dict) else {}

    def search(self, query: str) -> list[dict[str, Any]]:
        payload = self.get_json("/documents/search", {"include": query})
        return payload.get("items", []) if isinstance(payload, dict) else []


# Craft wraps content in its own structural tags (see "Craft Markdown
# Extensions"). They carry no meaning for a language model, so unwrap them and
# keep the text inside.
_STRUCTURAL_TAGS = ("page", "pageTitle", "content", "card", "callout", "caption")
_UNWRAP_RE = re.compile(
    r"</?(?:" + "|".join(_STRUCTURAL_TAGS) + r")(?:\s[^>]*)?/?>", re.IGNORECASE
)
_HIGHLIGHT_RE = re.compile(r"<highlight[^>]*>(.*?)</highlight>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<comment[^>]*>(.*?)</comment>", re.IGNORECASE | re.DOTALL)


def tidy_craft_markdown(text: str) -> str:
    """Unwrap Craft's structural tags, keeping the prose inside them."""
    text = _HIGHLIGHT_RE.sub(r"==\1==", text)
    text = _COMMENT_RE.sub(r"\1", text)
    text = _UNWRAP_RE.sub("", text)
    # Unwrapping leaves blank lines behind; collapse runs of 3+ into one break.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def blocks_to_markdown(block: Any, depth: int = 0) -> str:
    """Flatten Craft's nested block JSON into markdown."""
    if depth > 50 or not isinstance(block, dict):
        return ""
    parts = []
    markdown = block.get("markdown")
    if isinstance(markdown, str) and markdown.strip():
        parts.append(markdown.rstrip())
    for child in block.get("content") or []:
        nested = blocks_to_markdown(child, depth + 1)
        if nested:
            parts.append(nested)
    return "\n\n".join(parts)


def resolve_document(client: CraftREST, ref: str) -> tuple[str, str]:
    """Turn a user-supplied id or title into (document_id, title)."""
    documents = client.list_documents()
    if not documents:
        raise CraftConnError("This Craft connection exposes no documents.")

    for doc in documents:
        if doc.get("id") == ref:
            return ref, doc.get("title", ref)

    needle = ref.casefold()
    exact = [d for d in documents if (d.get("title") or "").casefold() == needle]
    partial = [d for d in documents if needle in (d.get("title") or "").casefold()]
    matches = exact or partial

    if len(matches) == 1:
        return matches[0]["id"], matches[0].get("title", matches[0]["id"])
    if not matches:
        available = ", ".join(repr(d.get("title")) for d in documents[:10])
        raise CraftConnError(f"No document matching {ref!r}. Available: {available}")
    titles = ", ".join(repr(d.get("title")) for d in matches[:10])
    raise CraftConnError(f"{ref!r} matches several documents: {titles}. Use an exact title or id.")


def fetch_craft_rest(
    endpoint: str,
    token: str | None,
    extra_headers: dict[str, str] | None,
    *,
    doc: str | None,
    search: str | None,
    folder: str | None,
    fetch_all: bool,
) -> str:
    client = CraftREST(endpoint, token, extra_headers)

    if folder:
        folder_id, name = resolve_folder(client, folder)
        documents = client.documents_in(folder_id)
        if not documents:
            raise CraftConnError(f"Folder {name!r} contains no documents.")
        print(f"Fetching {len(documents)} document(s) from {name!r}.", file=sys.stderr)
        return _concat(client, [d["id"] for d in documents if d.get("id")])

    if search:
        hits = client.search(search)
        if not hits:
            raise CraftConnError(f"No Craft documents matched {search!r}.")
        ids = []
        for hit in hits:
            doc_id = hit.get("documentId")
            if doc_id and doc_id not in ids:
                ids.append(doc_id)
        print(f"Search matched {len(ids)} document(s).", file=sys.stderr)
        return _concat(client, ids)

    if fetch_all:
        documents = client.list_documents()
        if not documents:
            raise CraftConnError("This Craft connection exposes no documents.")
        print(f"Fetching all {len(documents)} document(s).", file=sys.stderr)
        return _concat(client, [d["id"] for d in documents if d.get("id")])

    if not doc:
        raise CraftConnError(
            "Pick what to send: --doc <title or id>, --folder <name>, --search <query>, "
            "or --all. Run --list to see the space."
        )

    doc_id, title = resolve_document(client, doc)
    print(f"Fetching {title!r} ({doc_id}).", file=sys.stderr)
    body = client.document_markdown(doc_id)
    if not body:
        return ""
    first = body.lstrip().splitlines()[0].lstrip("# ").strip() if body.strip() else ""
    if first.casefold() == (title or "").casefold():
        return body  # Craft already rendered the title as the opening line
    return f"# {title}\n\n{body}"


def _concat(client: CraftREST, document_ids: list[str]) -> str:
    chunks = []
    for doc_id in document_ids:
        body = client.document_markdown(doc_id)
        if body:
            chunks.append(body)
    return "\n\n---\n\n".join(chunks)


def _folder_tree(client: CraftREST, node: dict[str, Any]) -> dict[str, Any]:
    """Build one folder's subtree, attributing each document to its deepest folder."""
    node_id = node.get("id", "")
    here = client.documents_in(node_id)
    children = [_folder_tree(client, child) for child in node.get("folders") or []]

    # documents_in() recurses, so subtract what the subfolders already claimed.
    claimed = {d.get("id") for child in children for d in child["all"]}
    return {
        "id": node_id,
        "name": node.get("name") or node_id,
        "direct": [d for d in here if d.get("id") not in claimed],
        "children": children,
        "all": here,
    }


def _render_tree(node: dict[str, Any], lines: list[str], width: int, depth: int = 0) -> None:
    pad = "  " * depth
    count = len(node["all"])
    lines.append(f"{pad}{node['name']}/  ({count} document{'s' if count != 1 else ''})")
    for doc in node["direct"]:
        doc_id = str(doc.get("id", ""))
        lines.append(f"{pad}  {doc_id:<{width}}  {doc.get('title', '(untitled)')}")
    for child in node["children"]:
        _render_tree(child, lines, width, depth + 1)


def list_craft_documents(
    endpoint: str,
    token: str | None,
    extra_headers: dict[str, str] | None,
    *,
    include_trash: bool = False,
) -> str:
    client = CraftREST(endpoint, token, extra_headers)

    try:
        space = client.connection_info().get("space", {}).get("name")
    except CraftConnError:
        space = None

    try:
        folders = client.list_folders()
    except CraftConnError:
        folders = []  # connection may not expose /folders; fall back to flat

    if not folders:
        documents = client.list_documents()
        if not documents:
            return "This Craft connection exposes no documents."
        width = max(len(str(d.get("id", ""))) for d in documents)
        lines = [f"{len(documents)} document(s)" + (f" in space {space!r}" if space else ""), ""]
        for doc in documents:
            lines.append(f"  {str(doc.get('id','')):<{width}}  {doc.get('title','(untitled)')}")
        return "\n".join(lines)

    skipped = None
    if not include_trash:
        keep = []
        for folder in folders:
            if folder.get("id") == "trash":
                skipped = folder.get("documentCount", 0)
            else:
                keep.append(folder)
        folders = keep

    trees = [_folder_tree(client, folder) for folder in folders]
    # Craft's built-in locations cannot be deleted, so an empty one is permanent
    # noise. A user-made folder that is empty is a deliberate choice; keep it.
    trees = [
        t for t in trees if t["all"] or t["id"] not in BUILTIN_LOCATIONS
    ]
    every = [d for tree in trees for d in tree["all"]]
    total = len({d.get("id") for d in every})
    width = max((len(str(d.get("id", ""))) for d in every), default=0)

    lines = [f"{total} document(s)" + (f" in space {space!r}" if space else ""), ""]
    for tree in trees:
        _render_tree(tree, lines, width)
        lines.append("")
    if skipped:
        lines.append(f"({skipped} in Recently Deleted; --include-trash to show)")
    return "\n".join(lines).rstrip()


def page_outline(block: Any, depth: int = 0, _out: list | None = None) -> list[tuple[int, str]]:
    """Walk a document's blocks, collecting its nested pages as (depth, title)."""
    out = [] if _out is None else _out
    if not isinstance(block, dict) or depth > 20:
        return out
    for child in block.get("content") or []:
        if isinstance(child, dict) and child.get("type") == "page":
            title = tidy_craft_markdown(str(child.get("markdown") or "")).strip()
            title = title.splitlines()[0] if title else "(untitled page)"
            out.append((depth, title))
            page_outline(child, depth + 1, out)
        else:
            page_outline(child, depth, out)
    return out


def tree_craft_documents(
    endpoint: str, token: str | None, extra_headers: dict[str, str] | None
) -> str:
    """List documents along with the sub-pages nested inside each one."""
    client = CraftREST(endpoint, token, extra_headers)
    documents = client.list_documents()
    if not documents:
        return "This Craft connection exposes no documents."

    try:
        space = client.connection_info().get("space", {}).get("name")
    except CraftConnError:
        space = None

    lines = [f"{len(documents)} document(s)" + (f" in space {space!r}" if space else ""), ""]
    for doc in documents:
        doc_id = doc.get("id", "")
        lines.append(f"{doc.get('title', '(untitled)')}   [{doc_id}]")
        try:
            pages = page_outline(client.blocks_json(doc_id))
        except CraftConnError as exc:
            lines.append(f"    (could not read: {exc})")
            pages = []
        for depth, title in pages:
            lines.append(f"    {'  ' * depth}- {title}")
        lines.append("")
    return "\n".join(lines).rstrip()


def resolve_folder(client: CraftREST, ref: str) -> tuple[str, str]:
    """Turn a user-supplied folder id or name into (folder_id, name)."""

    def walk(nodes):
        for node in nodes:
            yield node
            yield from walk(node.get("folders") or [])

    folders = list(walk(client.list_folders()))
    if not folders:
        raise CraftConnError("This Craft connection exposes no folders.")

    for folder in folders:
        if folder.get("id") == ref:
            return ref, folder.get("name", ref)

    needle = ref.casefold()
    exact = [f for f in folders if (f.get("name") or "").casefold() == needle]
    partial = [f for f in folders if needle in (f.get("name") or "").casefold()]
    matches = exact or partial

    if len(matches) == 1:
        return matches[0].get("id", ""), matches[0].get("name", ref)
    if not matches:
        names = ", ".join(repr(f.get("name")) for f in folders[:10])
        # Craft shows documents-with-subpages like folders in the sidebar, so a
        # "folder" the API does not know is very often a document.
        try:
            titles = {(d.get("title") or "").casefold(): d for d in client.list_documents()}
        except CraftConnError:
            titles = {}
        hit = titles.get(needle) or next(
            (d for t, d in titles.items() if needle in t), None
        )
        if hit is not None:
            raise CraftConnError(
                f"{ref!r} is a document, not a folder -- Craft displays documents that "
                f"contain sub-pages like folders. Use: --doc {hit.get('title')!r} "
                "(sub-pages are included automatically). "
                f"Actual folders: {names}"
            )
        raise CraftConnError(f"No folder matching {ref!r}. Available: {names}")
    names = ", ".join(repr(f.get("name")) for f in matches[:10])
    raise CraftConnError(f"{ref!r} matches several folders: {names}. Use an exact name or id.")


# --------------------------------------------------------------------------
# Craft: MCP connection
# --------------------------------------------------------------------------


class _FileTokenStorage:
    """Persist OAuth tokens per endpoint so you only authorize once."""

    def __init__(self, endpoint: str) -> None:
        slug = urlparse(endpoint).netloc.replace(":", "_") or "craft"
        self._path = CONFIG_DIR / f"oauth-{slug}.json"

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))
        self._path.chmod(0o600)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._load().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        data = self._load()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save(data)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(data)


class _CallbackCatcher:
    """One-shot localhost server that captures the OAuth redirect."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.result: dict[str, str] = {}
        self._done = threading.Event()

        catcher = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                query = parse_qs(urlparse(self.path).query)
                catcher.result = {k: v[0] for k, v in query.items()}
                body = b"<html><body><h2>Craft authorized. You can close this tab.</h2></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                catcher._done.set()

            def log_message(self, *args: Any) -> None:
                pass  # keep the console clean

        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_CallbackCatcher":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait(self, timeout: float = 300.0) -> dict[str, str]:
        if not self._done.wait(timeout):
            raise CraftConnError("Timed out waiting for Craft authorization in the browser.")
        return self.result


class _AuthedStreamableTransport:
    """MCP streamable-HTTP transport carrying our auth (OAuth or bearer token).

    The stock StreamableHTTPTransport takes only a URL, so we wrap the
    lower-level client to inject an httpx client that knows our credentials.
    """

    def __init__(self, url: str, *, auth: Any = None, headers: dict[str, str] | None = None):
        self._url = url
        self._auth = auth
        self._headers = headers or {}
        self._cm = None
        self._http = None

    async def __aenter__(self):
        from mcp.client.streamable_http import httpx2, streamable_http_client

        self._http = httpx2.AsyncClient(
            auth=self._auth, headers=self._headers, follow_redirects=True, timeout=120.0
        )
        self._cm = streamable_http_client(self._url, http_client=self._http)
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc: Any):
        try:
            if self._cm is not None:
                return await self._cm.__aexit__(*exc)
        finally:
            if self._http is not None:
                await self._http.aclose()


def _build_oauth(endpoint: str):
    from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    redirect_uri = f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}/callback"
    catcher = _CallbackCatcher(OAUTH_CALLBACK_PORT)

    async def redirect_handler(authorization_url: str) -> None:
        print(f"Opening browser to authorize Craft access:\n  {authorization_url}\n")
        webbrowser.open(authorization_url)

    async def callback_handler() -> AuthorizationCodeResult:
        params = await asyncio.to_thread(catcher.wait)
        if "error" in params:
            raise CraftConnError(f"Craft authorization failed: {params['error']}")
        if "code" not in params:
            raise CraftConnError("Craft authorization returned no code.")
        return AuthorizationCodeResult(
            code=params["code"], state=params.get("state"), iss=params.get("iss")
        )

    provider = OAuthClientProvider(
        server_url=endpoint,
        client_metadata=OAuthClientMetadata(
            client_name="craft-conn",
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=_FileTokenStorage(endpoint),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    return provider, catcher


@asynccontextmanager
async def _craft_mcp_client(endpoint: str, token: str | None):
    from mcp import Client

    if token:
        # A static credential from Craft's Connections tab skips the OAuth dance.
        transport = _AuthedStreamableTransport(
            endpoint, headers={"Authorization": f"Bearer {token}"}
        )
        async with Client(transport, raise_exceptions=True) as client:
            yield client
        return

    provider, catcher = _build_oauth(endpoint)
    with catcher:
        transport = _AuthedStreamableTransport(endpoint, auth=provider)
        async with Client(transport, raise_exceptions=True) as client:
            yield client


def _pick_tool(tools: list[Any], hints: tuple[str, ...]) -> Any | None:
    """Choose the tool whose name best matches our intent hints."""
    for hint in hints:
        for tool in tools:
            if tool.name == hint:
                return tool
    for hint in hints:
        for tool in tools:
            if hint in tool.name:
                return tool
    return None


def _result_text(result: Any) -> str:
    """Pull text out of an MCP CallToolResult across SDK shapes."""
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if structured:
        text = extract_text(structured)
        if text:
            return text

    chunks = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


async def fetch_craft_mcp(
    endpoint: str,
    token: str | None,
    *,
    doc: str | None,
    tool_name: str | None,
    list_only: bool,
) -> str:
    """Connect to Craft's MCP endpoint and pull document content."""
    async with _craft_mcp_client(endpoint, token) as client:
        tools = list((await client.list_tools()).tools)
        if not tools:
            raise CraftConnError("Craft's MCP server exposed no tools.")

        if list_only:
            lines = [f"Tools exposed by {endpoint}:", ""]
            for tool in tools:
                lines.append(f"  {tool.name}  -- {(tool.description or '').strip()}")
            return "\n".join(lines)

        if tool_name:
            tool = next((t for t in tools if t.name == tool_name), None)
            if tool is None:
                names = ", ".join(t.name for t in tools)
                raise CraftConnError(f"No MCP tool named {tool_name!r}. Available: {names}")
        else:
            hints = READ_TOOL_HINTS if doc else LIST_TOOL_HINTS
            tool = _pick_tool(tools, hints)
            if tool is None:
                names = ", ".join(t.name for t in tools)
                raise CraftConnError(
                    f"Could not guess which Craft tool to call. Pick one with --tool. Available: {names}"
                )

        arguments = _build_arguments(tool, doc)
        print(f"Calling Craft MCP tool {tool.name}({json.dumps(arguments)})...")
        result = await client.call_tool(tool.name, arguments)

        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            raise CraftConnError(f"Craft tool {tool.name} failed: {_result_text(result)}")

        text = _result_text(result)
        if not text:
            raise CraftConnError(f"Craft tool {tool.name} returned no readable content.")
        return text


def _build_arguments(tool: Any, doc: str | None) -> dict[str, Any]:
    """Fill a tool's required arguments as well as we can from --doc."""
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    properties = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []

    arguments: dict[str, Any] = {}
    if doc:
        # Prefer an explicit id/title/query parameter, whatever this tool calls it.
        for key in ("documentId", "document_id", "id", "query", "search", "title", "name"):
            if key in properties:
                arguments[key] = doc
                break
        else:
            if required:
                arguments[required[0]] = doc

    for key in required:
        if key in arguments:
            continue
        spec = properties.get(key, {})
        kind = spec.get("type")
        if kind == "string":
            arguments[key] = ""
        elif kind in ("number", "integer"):
            arguments[key] = 0
        elif kind == "boolean":
            arguments[key] = False
        elif kind == "array":
            arguments[key] = []
        elif kind == "object":
            arguments[key] = {}
    return arguments


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------


def build_gemini_client(project: str | None, location: str):
    """Vertex AI when a GCP project is given, otherwise the Gemini API key."""
    try:
        from google import genai
    except ImportError as exc:
        raise CraftConnError(
            "google-genai is not installed. Install it with:\n"
            "  uv pip install --python .venv/bin/python google-genai\n"
            "(the PyPI package named `google` is a different, unrelated project)"
        ) from exc

    # We never use automatic function calling; silence the SDK's blanket warning.
    logging.getLogger("google_genai.models").setLevel(logging.ERROR)

    if project:
        return genai.Client(vertexai=True, project=project, location=location)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise CraftConnError(
            "No credentials. Either export GEMINI_API_KEY, or pass --project "
            "<gcp-project> to route through Vertex AI with application default credentials."
        )
    return genai.Client(api_key=api_key)


def list_gemini_models(client) -> str:
    """Ask Gemini which models this key can actually use.

    Model ids churn fast and old ones are retired, so read the live list rather
    than trusting anything hardcoded here.
    """
    try:
        models = list(client.models.list())
    except Exception as exc:
        raise CraftConnError(f"Could not list Gemini models: {exc}") from exc

    usable = []
    for model in models:
        actions = getattr(model, "supported_actions", None) or []
        if not actions or "generateContent" in actions:
            usable.append(model)

    if not usable:
        return "This key exposes no models that support generateContent."

    lines = [f"{len(usable)} model(s) available to this key:", ""]
    for model in sorted(usable, key=lambda m: getattr(m, "name", "")):
        name = (getattr(model, "name", "") or "").removeprefix("models/")
        display = getattr(model, "display_name", "") or ""
        lines.append(f"  {name:<34}  {display}")
    lines.append("")
    lines.append("Pass one with --model, or set GEMINI_MODEL.")
    return "\n".join(lines)


def analyze_with_gemini(client, document_text: str, prompt: str, model: str) -> str:
    contents = f"{prompt.strip()}\n\n--- BEGIN CRAFT DOCUMENT ---\n{document_text}\n--- END CRAFT DOCUMENT ---"
    try:
        response = client.models.generate_content(model=model, contents=contents)
    except Exception as exc:
        detail = str(exc)
        if "404" in detail or "not found" in detail.lower():
            raise CraftConnError(
                f"Gemini rejected the model {model!r}; it may have been retired. "
                "Run --list-models to see what this key can use."
            ) from exc
        raise CraftConnError(f"Gemini request failed: {detail}") from exc

    text = getattr(response, "text", None)
    if not text:
        raise CraftConnError(f"Gemini returned no text (finish reason: {response!r}).")
    return text


# --------------------------------------------------------------------------
# iTerm2 presentation
# --------------------------------------------------------------------------


def show_in_iterm2(path: Path) -> None:
    """When launched from iTerm2's Scripts menu there is no console, so open one."""

    async def _main(connection):
        app = await iterm2.async_get_app(connection)
        window = app.current_window
        if window is None:
            window = await iterm2.Window.async_create(connection)
        else:
            await window.async_create_tab()
        session = window.current_tab.current_session
        await session.async_send_text(f"${{PAGER:-less}} {json.dumps(str(path))}\n")

    iterm2.run_until_complete(_main)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a Craft document to Google Gemini for analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    craft = parser.add_argument_group("Craft")
    craft.add_argument(
        "--endpoint",
        default=os.environ.get("CRAFT_API_ENDPOINT"),
        help="Craft endpoint URL from the Connections tab [env: CRAFT_API_ENDPOINT]",
    )
    craft.add_argument(
        "--token",
        default=os.environ.get("CRAFT_API_TOKEN"),
        help="Craft credential; omit for MCP to run the OAuth flow [env: CRAFT_API_TOKEN]",
    )
    craft.add_argument(
        "--mode",
        choices=("auto", "rest", "mcp"),
        default="auto",
        help="Connection kind; 'auto' infers it from the URL (default: auto)",
    )
    craft.add_argument("--doc", help="Document id or title to send")
    craft.add_argument("--search", help="Send every document matching this query")
    craft.add_argument("--folder", help="Send every document in this folder (recursive)")
    craft.add_argument(
        "--all", action="store_true", dest="fetch_all", help="Send every document in the space"
    )
    craft.add_argument("--tool", help="Exact MCP tool to call, overriding the heuristic")
    craft.add_argument(
        "--list",
        action="store_true",
        dest="list_items",
        help="List documents (REST) or tools (MCP) and exit",
    )
    craft.add_argument("--list-tools", action="store_true", help=argparse.SUPPRESS)
    craft.add_argument(
        "--include-trash", action="store_true", help="Include Recently Deleted in --list"
    )
    craft.add_argument(
        "--tree",
        action="store_true",
        help="List documents with the sub-pages nested inside each (REST mode)",
    )
    craft.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="'Name: value'",
        help="Exact auth header to send, repeatable; overrides --token guessing (REST mode)",
    )

    gem = parser.add_argument_group("Gemini")
    gem.add_argument(
        "--project",
        default=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        help="Google Cloud project; routes through Vertex AI [env: GOOGLE_CLOUD_PROJECT]",
    )
    gem.add_argument(
        "--location",
        default=os.environ.get("GOOGLE_CLOUD_LOCATION", DEFAULT_LOCATION),
        help=f"Vertex AI location (default: {DEFAULT_LOCATION})",
    )
    gem.add_argument("--model", default=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
    gem.add_argument(
        "--list-models",
        action="store_true",
        help="List Gemini models this key can use, and exit",
    )
    gem.add_argument("--prompt", help="Instruction sent alongside the document")
    gem.add_argument("--prompt-file", type=Path, help="Read the instruction from a file")
    gem.add_argument(
        "--max-chars",
        type=int,
        default=800_000,
        help="Truncate the document at this many characters (default: 800000)",
    )

    parser.add_argument("--out", type=Path, help="Write the analysis here instead of stdout")
    parser.add_argument(
        "--dry-run", action="store_true", help="Fetch from Craft and print it; skip Gemini"
    )
    return parser.parse_args(argv)


def parse_headers(raw: list[str]) -> dict[str, str]:
    headers = {}
    for item in raw:
        name, sep, value = item.partition(":")
        if not sep or not name.strip():
            raise CraftConnError(f"Bad --header {item!r}; expected 'Name: value'.")
        headers[name.strip()] = value.strip()
    return headers


def resolve_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return args.prompt_file.read_text()
    if args.prompt:
        return args.prompt
    return DEFAULT_PROMPT


def run(args: argparse.Namespace) -> str:
    if args.list_models:
        return list_gemini_models(build_gemini_client(args.project, args.location))

    if not args.endpoint:
        raise CraftConnError(
            "No Craft endpoint. Copy it from Craft > Connections and either pass "
            "--endpoint or export CRAFT_API_ENDPOINT."
        )

    mode = args.mode
    if mode == "auto":
        mode = "mcp" if _looks_like_mcp(args.endpoint) else "rest"

    headers = parse_headers(args.header)

    if args.list_models:
        return list_gemini_models(build_gemini_client(args.project, args.location))

    if args.tree:
        if mode != "rest":
            raise CraftConnError("--tree only applies to REST connections.")
        return tree_craft_documents(args.endpoint, args.token, headers)

    if args.list_items or args.list_tools:
        if mode == "mcp":
            return asyncio.run(
                fetch_craft_mcp(
                    args.endpoint, args.token, doc=None, tool_name=None, list_only=True
                )
            )
        return list_craft_documents(
            args.endpoint, args.token, headers, include_trash=args.include_trash
        )

    print(f"Fetching from Craft ({mode}): {args.endpoint}", file=sys.stderr)
    if mode == "mcp":
        document = asyncio.run(
            fetch_craft_mcp(
                args.endpoint,
                args.token,
                doc=args.doc,
                tool_name=args.tool,
                list_only=False,
            )
        )
    else:
        document = fetch_craft_rest(
            args.endpoint,
            args.token,
            headers,
            doc=args.doc,
            search=args.search,
            folder=args.folder,
            fetch_all=args.fetch_all,
        )

    if not document.strip():
        raise CraftConnError("Craft returned an empty document.")

    if len(document) > args.max_chars:
        print(
            f"Document is {len(document)} chars; truncating to {args.max_chars}.",
            file=sys.stderr,
        )
        document = document[: args.max_chars]

    if args.dry_run:
        return document

    target = f"Vertex AI project {args.project}" if args.project else "the Gemini API"
    print(f"Sending {len(document)} chars to {args.model} via {target}...", file=sys.stderr)
    client = build_gemini_client(args.project, args.location)
    return analyze_with_gemini(client, document, resolve_prompt(args), args.model)


def _unwrap(exc: BaseException) -> BaseException:
    """Dig a CraftConnError out of the ExceptionGroup anyio wraps it in."""
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _unwrap(inner)
            if isinstance(found, CraftConnError):
                return found
    return exc


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        output = run(args)
    except BaseExceptionGroup as group:
        unwrapped = _unwrap(group)
        if not isinstance(unwrapped, CraftConnError):
            raise
        print(f"error: {unwrapped}", file=sys.stderr)
        return 1
    except CraftConnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output)
        print(f"Wrote {args.out}", file=sys.stderr)
    elif iterm2 is not None and os.environ.get("ITERM2_COOKIE"):
        # Launched from the Scripts menu: stdout is not visible anywhere.
        scratch = CONFIG_DIR / "last-analysis.md"
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(output)
        show_in_iterm2(scratch)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
