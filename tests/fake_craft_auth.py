"""Fake Craft REST server that accepts only ONE auth scheme, chosen by argv."""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

DOC = {"title": "Siege of Vantar", "content": [{"text": "Draft siege cards in turn."}]}
SCHEME = sys.argv[2]

CHECKS = {
    "bearer":            lambda h: h.get("Authorization") == "Bearer tok",
    "x-api-key":         lambda h: h.get("X-API-Key") == "tok",
    "raw-authorization": lambda h: h.get("Authorization") == "tok",
    "api-key":           lambda h: h.get("Api-Key") == "tok",
    "never":             lambda h: False,
}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if not CHECKS[SCHEME](self.headers):
            self.send_response(401); self.end_headers()
            self.wfile.write(b'{"error":"bad credential"}'); return
        b = json.dumps(DOC).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
