"""Fake Craft API with a folder hierarchy, mirroring REWILD's shape."""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

BASE = "/links/TEST/api/v1"
# folderId -> documents directly inside
DIRECT = {
    "unsorted":  [("d-getting","Getting Started 👋"), ("d-handbook","Craft Handbook 📖")],
    "templates": [],
    "trash":     [("d-old","Old Draft")],
    "f-pitch":   [("d-pitch","The Pitch: REWILD"), ("d-onepager","One Pager")],
    "f-plan":    [("d-plan","The Plan")],
    "f-plan-q1": [("d-q1","Q1 Milestones")],
    "f-res":     [("d-research","Research"), ("d-rulebook","Rulebook")],
}
CHILDREN = {"f-plan": ["f-plan-q1"]}
FOLDERS = [
    {"id":"unsorted","name":"Unsorted","documentCount":2,"folders":[]},
    {"id":"trash","name":"Recently Deleted","documentCount":1,"folders":[]},
    {"id":"templates","name":"Templates","documentCount":0,"folders":[]},
    {"id":"f-pitch","name":"The Pitch: REWILD","documentCount":2,"folders":[]},
    {"id":"f-plan","name":"The Plan","documentCount":2,"folders":[
        {"id":"f-plan-q1","name":"Q1","documentCount":1,"folders":[]}]},
    {"id":"f-res","name":"Research","documentCount":2,"folders":[]},
]

def recursive(fid):
    out = list(DIRECT.get(fid, []))
    for c in CHILDREN.get(fid, []): out += recursive(c)
    return out

ALL = [d for f in ("unsorted","templates","f-pitch","f-plan","f-res") for d in recursive(f)]

class H(BaseHTTPRequestHandler):
    def _s(self, code, body, ct="application/json"):
        b=body.encode() if isinstance(body,str) else body
        self.send_response(code); self.send_header("Content-Type",ct)
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.headers.get("Authorization")!="Bearer tok": return self._s(401,'{"e":"unauth"}')
        u=urlparse(self.path); q=parse_qs(u.query)
        path=u.path[len(BASE):] if u.path.startswith(BASE) else u.path
        def items(pairs): return json.dumps({"items":[{"id":i,"title":t} for i,t in pairs]})
        if path=="/connection": return self._s(200,json.dumps({"space":{"id":"s","name":"REWILD"}}))
        if path=="/folders": return self._s(200,json.dumps({"items":FOLDERS}))
        if path=="/documents":
            if "folderId" in q: return self._s(200, items(recursive(q["folderId"][0])))
            if "location" in q: return self._s(200, items(recursive(q["location"][0])))
            return self._s(200, items(ALL))
        if path=="/blocks":
            did=q.get("id",[None])[0]
            title=dict(ALL+DIRECT["trash"]).get(did)
            if not title: return self._s(404,'{"e":"nf"}')
            return self._s(200, f"<page>{title}</page>\n\nBody of {title}.", "text/markdown")
        return self._s(404,'{"e":"nf"}')
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1",int(sys.argv[1])),H).serve_forever()
