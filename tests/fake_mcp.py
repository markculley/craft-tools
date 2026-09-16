"""Fake Craft REST + MCP servers for testing craft-conn."""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

DOC = {"document": {"title": "Siege of Vantar", "content": [
    {"text": "Players take turns drafting siege cards."},
    {"text": "A tie is resolved by the player with fewer supply tokens."}]}}

class RestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer secret-token":
            self.send_response(401); self.end_headers(); self.wfile.write(b"nope"); return
        body = json.dumps(DOC).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

def run_rest(port):
    HTTPServer(("127.0.0.1", port), RestHandler).serve_forever()

def run_mcp(port):
    from mcp.server.mcpserver import MCPServer
    server = MCPServer(name="fake-craft")
    @server.tool()
    def get_document(documentId: str) -> str:
        """Read a Craft document by id."""
        return f"[{documentId}] " + " ".join(b["text"] for b in DOC["document"]["content"])
    @server.tool()
    def list_documents() -> str:
        """List Craft documents."""
        return "Siege of Vantar (id: doc-1)"
    import uvicorn
    uvicorn.run(server.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning")

if __name__ == "__main__":
    which, port = sys.argv[1], int(sys.argv[2])
    (run_rest if which == "rest" else run_mcp)(port)
