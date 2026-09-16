"""Fake Craft API shaped like REWILD: no user folders, docs with nested pages."""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
BASE="/links/TEST/api/v1"
FOLDERS=[{"id":"unsorted","name":"Unsorted","documentCount":7,"folders":[]},
         {"id":"daily_notes","name":"Daily Notes","documentCount":0,"folders":[]},
         {"id":"trash","name":"Recently Deleted","documentCount":0,"folders":[]},
         {"id":"templates","name":"Templates","documentCount":0,"folders":[]}]
DOCS=[("d-research","Research"),("d-rulebook","Rulebook"),("d-plan","The Plan"),
      ("d-pitch","The Pitch: REWILD"),("d-videos","How To Videos 📺")]
def page(t,kids=None):
    b={"id":t,"type":"page","markdown":f"<page>{t}</page>","content":[
        {"id":t+"-x","type":"text","markdown":f"Prose inside {t}."}]}
    for k in (kids or []): b["content"].append(k)
    return b
BLOCKS={
 "d-pitch": page("The Pitch: REWILD",[page("Problem"),page("Solution",[page("Mechanics")])]),
 "d-plan":  page("The Plan",[page("Q1"),page("Q2")]),
 "d-research": page("Research"), "d-rulebook": page("Rulebook"), "d-videos": page("How To Videos 📺"),
}
def md(b,d=0):
    out=[b.get("markdown","")]
    for c in b.get("content",[]): out.append(md(c,d+1))
    return "\n\n".join(x for x in out if x)
class H(BaseHTTPRequestHandler):
    def _s(self,c,b,ct="application/json"):
        bb=b.encode() if isinstance(b,str) else b
        self.send_response(c); self.send_header("Content-Type",ct)
        self.send_header("Content-Length",str(len(bb))); self.end_headers(); self.wfile.write(bb)
    def do_GET(self):
        if self.headers.get("Authorization")!="Bearer tok": return self._s(401,'{"e":"u"}')
        u=urlparse(self.path); q=parse_qs(u.query)
        path=u.path[len(BASE):] if u.path.startswith(BASE) else u.path
        if path=="/connection": return self._s(200,json.dumps({"space":{"name":"REWILD"}}))
        if path=="/folders": return self._s(200,json.dumps({"items":FOLDERS}))
        if path=="/documents":
            if "folderId" in q: return self._s(200,json.dumps({"items":[]}))
            loc=q.get("location",["unsorted"])[0]
            items=[{"id":i,"title":t} for i,t in DOCS] if loc=="unsorted" else []
            return self._s(200,json.dumps({"items":items}))
        if path=="/blocks":
            did=q.get("id",[None])[0]
            if did not in BLOCKS: return self._s(404,'{"e":"nf"}')
            if "text/markdown" in self.headers.get("Accept",""):
                return self._s(200, md(BLOCKS[did]), "text/markdown")
            return self._s(200, json.dumps(BLOCKS[did]))
        return self._s(404,'{"e":"nf"}')
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1",int(sys.argv[1])),H).serve_forever()
