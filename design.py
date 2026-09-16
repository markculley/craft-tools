#!/usr/bin/env python3
"""Interactive design sessions over Craft documents, with standing research context.

One document is the draft under design; everything else is background the model
should reason *from* but not rewrite. Research stays loaded across turns, so you
can argue about a mechanic and have the source notes already in context.

    ./design.py --draft "Rulebook v0.1" --context Research

Reuses craft-conn.py for Craft access, so there is one tested client.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE = pathlib.Path(__file__).resolve().parent
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


def _load_craft_conn():
    """Import craft-conn.py, whose hyphenated name blocks a normal import."""
    path = _HERE / "craft-conn.py"
    if not path.exists():
        sys.exit(f"Cannot find {path}; design.py reuses its Craft client.")
    spec = importlib.util.spec_from_file_location("craft_conn", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["craft_conn"] = module
    spec.loader.exec_module(module)
    return module


cc = _load_craft_conn()
CraftConnError = cc.CraftConnError

SESSION_DIR = cc.CONFIG_DIR / "sessions"

DESIGN_PROMPT = """\
You are a design collaborator on a tabletop game, not a reviewer writing a report.

You will be given one DRAFT document and some RESEARCH documents. The draft is
the thing being designed and is expected to change. The research is background:
reason from it, cite it when it is relevant, but do not propose rewriting it.

How to work:
- Argue for positions rather than listing balanced options. Say what you think.
- When you disagree, say so plainly and give the reasoning.
- Change your mind when the counter-argument is better; say that you have.
- Prefer one concrete alternative over three vague ones.
- Keep replies short unless depth is asked for. This is a conversation.
"""

OPENING = """\
Read the material, then open with a short orientation: what the draft is trying
to do, and the two or three tensions you think most warrant argument. Do not
review it exhaustively -- we will get there by talking.
"""

BANNER = """\
  /add <name>       load another Craft document or folder into context
  /drop <name>      remove a loaded document
  /context          what is loaded, and how large
  /craft [title]    synthesise the decisions and write them back to Craft
  /save [path]      write this transcript to a file
  /history          turns so far
  /quit             end session (or Ctrl-D)
"""


# --------------------------------------------------------------------------
# Craft: reading the working set, and writing conclusions back
# --------------------------------------------------------------------------


class CraftWorkspace(cc.CraftREST):
    """Adds the write half of the Craft API to craft-conn's read-only client."""

    def _prime_auth(self) -> None:
        # get() discovers the working auth header on its first call; POST reuses it.
        if self._auth is None:
            self.get_json("/connection")

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        self._prime_auth()
        self._throttle()
        url = f"{self.base}{path}"
        try:
            response = self.session.post(
                url,
                headers={"Accept": "application/json", "Content-Type": "application/json", **(self._auth or {})},
                json=payload,
                timeout=60,
            )
        except Exception as exc:
            raise CraftConnError(f"Unable to reach Craft at {url}: {exc}") from exc

        if not response.ok:
            body = (response.text or "").strip()[:200]
            raise CraftConnError(f"Craft returned HTTP {response.status_code} for {path}. {body}")
        try:
            return response.json()
        except ValueError:
            return {}

    def create_document(self, title: str, markdown: str, folder_id: str | None = None) -> str:
        """Create a document and fill it. Always new -- never overwrites."""
        payload: dict[str, Any] = {"documents": [{"title": title}]}
        if folder_id:
            payload["destination"] = {"folderId": folder_id}
        created = self._post("/documents", payload)
        items = created.get("items") or []
        if not items or not items[0].get("id"):
            raise CraftConnError(f"Craft did not return an id for the new document: {created}")

        doc_id = items[0]["id"]
        self._post("/blocks", {"markdown": markdown, "position": {"position": "end", "pageId": doc_id}})
        return doc_id


def load_reference(client: CraftWorkspace, ref: str) -> list[tuple[str, str]]:
    """Resolve a name to (title, markdown) pairs -- a folder yields many."""
    try:
        folder_id, name = cc.resolve_folder(client, ref)
    except CraftConnError:
        folder_id = None
        name = ref

    if folder_id:
        documents = client.documents_in(folder_id)
        if not documents:
            raise CraftConnError(f"Folder {name!r} is empty.")
        out = []
        for doc in documents:
            body = client.document_markdown(doc["id"])
            if body:
                out.append((doc.get("title", doc["id"]), body))
        return out

    doc_id, title = cc.resolve_document(client, ref)
    body = client.document_markdown(doc_id)
    if not body:
        raise CraftConnError(f"Document {title!r} is empty.")
    return [(title, body)]


# --------------------------------------------------------------------------
# The working set
# --------------------------------------------------------------------------


class WorkingSet:
    """The draft under design plus the research standing behind it."""

    def __init__(self) -> None:
        self.draft: tuple[str, str] | None = None
        self.research: dict[str, str] = {}

    def set_draft(self, title: str, body: str) -> None:
        self.draft = (title, body)

    def add(self, title: str, body: str) -> None:
        self.research[title] = body

    def drop(self, needle: str) -> list[str]:
        hit = [t for t in self.research if needle.casefold() in t.casefold()]
        for title in hit:
            del self.research[title]
        return hit

    def as_prompt(self) -> str:
        parts = []
        if self.draft:
            title, body = self.draft
            parts.append(f"--- DRAFT UNDER DESIGN: {title} ---\n{body}\n--- END DRAFT ---")
        for title, body in self.research.items():
            parts.append(f"--- RESEARCH: {title} ---\n{body}\n--- END RESEARCH ---")
        return "\n\n".join(parts)

    def describe(self) -> str:
        lines = []
        if self.draft:
            title, body = self.draft
            lines.append(f"  draft      {title}  ({len(body):,} chars)")
        else:
            lines.append("  draft      (none)")
        for title, body in self.research.items():
            lines.append(f"  research   {title}  ({len(body):,} chars)")
        total = len(self.as_prompt())
        lines.append("")
        lines.append(f"  {total:,} chars of context (~{total // 4:,} tokens)")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


def stream_reply(chat, message: str) -> str:
    chunks = []
    try:
        for chunk in chat.send_message_stream(message):
            text = getattr(chunk, "text", None)
            if text:
                chunks.append(text)
                print(text, end="", flush=True)
    except Exception as exc:
        detail = str(exc)
        if "404" in detail or "not found" in detail.lower():
            raise CraftConnError(
                "Gemini rejected the model; run craft-conn.py --list-models for valid ids."
            ) from exc
        raise CraftConnError(f"Gemini request failed: {detail}") from exc
    print("\n")
    return "".join(chunks)


def transcript(turns: list[tuple[str, str]], title: str) -> str:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Design session: {title}", f"_{stamp}_", ""]
    for speaker, text in turns:
        lines.append(f"## {speaker}")
        lines.append("")
        lines.append(text.strip())
        lines.append("")
    return "\n".join(lines)


def save_session(name: str, turns: list[tuple[str, str]], working: WorkingSet) -> Path:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSION_DIR / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "saved": datetime.now(timezone.utc).isoformat(),
                "draft": working.draft[0] if working.draft else None,
                "research": list(working.research),
                "turns": turns,
            },
            indent=2,
        )
    )
    return path


def run_session(client: CraftWorkspace, gem, working: WorkingSet, model: str, prompt: str) -> None:
    try:
        import readline  # noqa: F401 -- line editing and history in input()
    except ImportError:
        pass

    chat = gem.chats.create(model=model)
    turns: list[tuple[str, str]] = []
    label = working.draft[0] if working.draft else "REWILD"

    print(f"\nDesign session on {label!r}   model: {model}")
    print(working.describe())
    print(BANNER)

    print("> (reading)\n")
    turns.append(("Gemini", stream_reply(chat, f"{prompt.strip()}\n\n{working.as_prompt()}\n\n{OPENING}")))

    while True:
        try:
            message = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue

        if message in ("/quit", "/exit", "/q"):
            break

        if message == "/history":
            print(f"{len(turns)} turns, {len(chat.get_history())} messages in context.\n")
            continue

        if message == "/context":
            print(working.describe() + "\n")
            continue

        if message.startswith("/add"):
            ref = message[4:].strip().strip('"')
            if not ref:
                print("Usage: /add <document or folder>\n")
                continue
            try:
                loaded = load_reference(client, ref)
            except CraftConnError as exc:
                print(f"{exc}\n")
                continue
            for title, body in loaded:
                working.add(title, body)
            names = ", ".join(t for t, _ in loaded)
            print(f"Loaded {names}.\n")
            # Tell the model in-band so it can use the new material immediately.
            block = "\n\n".join(f"--- RESEARCH: {t} ---\n{b}\n--- END RESEARCH ---" for t, b in loaded)
            turns.append(("Gemini", stream_reply(
                chat,
                f"Additional research has been added to our context:\n\n{block}\n\n"
                "Note briefly what in here bears on the draft. Do not re-summarise it.",
            )))
            continue

        if message.startswith("/drop"):
            ref = message[5:].strip().strip('"')
            dropped = working.drop(ref) if ref else []
            print(f"Dropped {', '.join(dropped)}.\n" if dropped else f"Nothing matching {ref!r}.\n")
            continue

        if message.startswith("/save"):
            parts = message.split(maxsplit=1)
            target = Path(parts[1]).expanduser() if len(parts) > 1 else SESSION_DIR / f"{_slug(label)}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(transcript(turns, label))
            print(f"Wrote {target}\n")
            continue

        if message.startswith("/craft"):
            title = message[6:].strip().strip('"')
            _write_back(client, chat, turns, label, title)
            continue

        if message.startswith("/"):
            print(f"Unknown command {message!r}.\n{BANNER}")
            continue

        turns.append(("Mark", message))
        turns.append(("Gemini", stream_reply(chat, message)))

    if turns:
        path = save_session(_slug(label), turns, working)
        print(f"Session saved to {path}")


def _slug(text: str) -> str:
    keep = [c if c.isalnum() or c in "-_" else "-" for c in text.lower()]
    return "".join(keep).strip("-")[:60] or "session"


def _write_back(client: CraftWorkspace, chat, turns: list[tuple[str, str]], label: str, title: str) -> None:
    """Ask for a synthesis of what was decided, then create a Craft document."""
    print("Synthesising decisions...\n")
    synthesis = stream_reply(
        chat,
        "Summarise this session as a design note: what we decided, what we rejected "
        "and why, and what is still open. Markdown, with headings. Decisions only -- "
        "no transcript, no recap of the draft.",
    )

    doc_title = title or f"Design notes: {label} ({datetime.now().strftime('%Y-%m-%d')})"
    answer = input(f'Create Craft document "{doc_title}"? [y/N] ').strip().lower()
    if answer not in ("y", "yes"):
        print("Not written.\n")
        return

    try:
        doc_id = client.create_document(doc_title, synthesis)
    except CraftConnError as exc:
        print(f"{exc}\n")
        return
    turns.append(("Gemini", synthesis))
    print(f"Created {doc_title!r} in Craft ({doc_id}).\n")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--draft", help="The document under design")
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        metavar="NAME",
        help="Document or folder to load as research; repeatable",
    )
    parser.add_argument("--endpoint", default=os.environ.get("CRAFT_API_ENDPOINT"))
    parser.add_argument("--token", default=os.environ.get("CRAFT_API_TOKEN"))
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--model", default=os.environ.get("GEMINI_MODEL", cc.DEFAULT_MODEL))
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"))
    parser.add_argument("--prompt-file", type=Path, help="Replace the collaborator framing")
    args = parser.parse_args(argv)

    try:
        if not args.endpoint:
            raise CraftConnError("Set CRAFT_API_ENDPOINT or pass --endpoint.")
        if not args.draft:
            raise CraftConnError("Pass --draft <document>: the thing being designed.")

        client = CraftWorkspace(args.endpoint, args.token, cc.parse_headers(args.header))
        working = WorkingSet()

        doc_id, title = cc.resolve_document(client, args.draft)
        body = client.document_markdown(doc_id)
        if not body:
            raise CraftConnError(f"Draft {title!r} is empty.")
        working.set_draft(title, body)

        for ref in args.context:
            for name, text in load_reference(client, ref):
                working.add(name, text)

        gem = cc.build_gemini_client(args.project, args.location)
        prompt = args.prompt_file.read_text() if args.prompt_file else DESIGN_PROMPT
        run_session(client, gem, working, args.model, prompt)
    except CraftConnError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
