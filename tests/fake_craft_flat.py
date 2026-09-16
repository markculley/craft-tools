"""Fake Craft API matching the documented /api/v1 contract."""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

BASE = "/links/TEST/api/v1"
DOCS = [{"id": "doc-123", "title": "Siege of Vantar"},
        {"id": "doc-456", "title": "Vantar Errata"},
        {"id": "doc-789", "title": "Shopping List"}]
BLOCKS = {
    "doc-123": {"id":"0","type":"page","markdown":"<page>Siege of Vantar</page>","content":[
        {"id":"1","type":"text","textStyle":"h1","markdown":"# Turn Order"},
        {"id":"2","type":"text","markdown":"Players draft siege cards in turn."},
        {"id":"3","type":"page","markdown":"Ties","content":[
            {"id":"4","type":"text","markdown":"Ties go to fewer supply tokens."}]}]},
    "doc-456": {"id":"0","type":"page","markdown":"<page>Vantar Errata</page>","content":[
        {"id":"1","type":"text","markdown":"Errata: rule 4.2 supersedes 3.1."}]},
    "doc-789": {"id":"0","type":"page","markdown":"<page>Shopping List</page>","content":[
        {"id":"1","type":"text","markdown":"- Milk"}]},
}
MARKDOWN = {k: "\n\n".join(
    [v["markdown"]] + [c["markdown"] for c in v.get("content",[])]) for k,v in BLOCKS.items()}
SPEC = "# Craft API\n\nThis is the OpenAPI documentation, not your content."

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body,str) else body
        self.send_response(code); self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer tok":
            return self._send(401, '{"error":"unauthorized"}')
        u = urlparse(self.path); q = parse_qs(u.query)
        path = u.path[len(BASE):] if u.path.startswith(BASE) else u.path
        accept = self.headers.get("Accept","")

        if path in ("", "/"):                       # bare base -> the spec
            return self._send(200, SPEC, "text/markdown")
        if path == "/connection":
            return self._send(200, json.dumps({"space":{"id":"s1","name":"Game Design"}}))
        if path == "/documents":
            return self._send(200, json.dumps({"items": DOCS}))
        if path == "/documents/search":
            term = (q.get("include",[""])[0]).lower()
            hits = [{"documentId": d["id"], "markdown": "..."} for d in DOCS
                    if term in json.dumps(BLOCKS[d["id"]]).lower() or term in d["title"].lower()]
            return self._send(200, json.dumps({"items": hits}))
        if path == "/blocks":
            did = q.get("id",[None])[0]
            if did not in BLOCKS: return self._send(404, '{"error":"no such block"}')
            if "text/markdown" in accept:
                return self._send(200, MARKDOWN[did], "text/markdown")
            return self._send(200, json.dumps(BLOCKS[did]))
        return self._send(404, '{"error":"not found"}')
    def log_message(self,*a): pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
