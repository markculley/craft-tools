import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
PORT=int(sys.argv[1])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/link/X/docs/v1"):
            b=b"# Craft API docs (public)"; self.send_response(200)
            self.send_header("Content-Type","text/markdown")
            self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b); return
        self.send_response(307)
        self.send_header("Location", f"http://127.0.0.1:{PORT}/link/X/docs/v1"); self.end_headers()
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1",PORT),H).serve_forever()
