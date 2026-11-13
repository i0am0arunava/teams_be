#!/usr/bin/env python3
"""Teams-clone backend (standard library only). Local: python simple.py   Render start command: python simple.py
Data is stored in db.json (+ uploaded files in ./uploads). Set DATA_DIR to a persistent disk path to keep data."""
import hashlib, json, os, re, secrets, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

DATA = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_FILE, FILE_DIR = os.path.join(DATA, "db.json"), os.path.join(DATA, "uploads")
os.makedirs(FILE_DIR, exist_ok=True)
MAX_FILE = 10 * 1024 * 1024
EMAIL = re.compile(r"^[^@\s'\"<>]+@[^@\s'\"<>]+\.[^@\s'\"<>]+$")

cv = threading.Condition()          # guards everything below; also wakes long-polls
db = {"users": {}, "groups": {}, "files": {}}
if os.path.exists(DB_FILE):
    with open(DB_FILE) as f:
        db.update(json.load(f))
events, seq, calls, seen = [], 0, {}, {}

def save():
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(db, f)
    os.replace(tmp, DB_FILE)

def emit(to, ev):
    global seq
    with cv:
        seq += 1
        events.append({**ev, "id": seq, "to": list(to)})
        del events[:-3000]
        cv.notify_all()

def now_ms(): return int(time.time() * 1000)
def sysmsg(g, text): g["msgs"].append({"id": uuid.uuid4().hex[:8], "sys": 1, "text": text, "ts": now_ms()})

def pub(e):
    u = db["users"][e]
    return {"name": u["name"], "email": e, "org": u["org"], "online": time.time() - seen.get(e, 0) < 35}

def live_calls():
    out = {}
    for gid, c in list(calls.items()):
        c["set"] = [e for e in c["set"] if time.time() - seen.get(e, 0) < 60]
        if c["set"]: out[gid] = {"mode": c["mode"], "set": list(c["set"])}
        else: del calls[gid]
    return out

def state(u):
    e = u["email"]
    return {"seq": seq, "me": pub(e), "calls": live_calls(),
            "users": [pub(k) for k, v in db["users"].items() if v["org"] == u["org"]],
            "groups": [{**g, "msgs": g["msgs"][-300:]} for g in db["groups"].values() if e in g["members"]]}

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    def out(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.cors()
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_OPTIONS(self):
        self.send_response(204); self.cors(); self.send_header("Content-Length", "0"); self.end_headers()
    def do_GET(self): self.run()
    def do_POST(self): self.run()

    def run(self):
        try: self.route()
        except Exception as ex: self.out({"error": str(ex)}, 500)

    def route(self):
        url = urlparse(self.path); q = parse_qs(url.query); p = url.path
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_FILE + 4096: return self.out({"error": "File too large (10 MB max)"}, 413)
        raw = self.rfile.read(n) if n else b""
        if p == "/": return self.out({"ok": True, "service": "teams-clone"})
        try: b = json.loads(raw) if raw and p != "/api/upload" else {}
        except ValueError: b = {}
        if p == "/api/login":
            name, email = str(b.get("name", "")).strip()[:60], str(b.get("email", "")).strip().lower()
            if not name or not EMAIL.match(email): return self.out({"error": "Enter a name and a valid work email"}, 400)
            org = email.split("@")[1]
            with cv:
                u = db["users"].setdefault(email, {"name": name, "org": org, "token": secrets.token_urlsafe(24)})
                u["name"] = name; seen[email] = time.time(); save()
                emit([k for k, v in db["users"].items() if v["org"] == org], {"type": "sync"})
            return self.out({"token": u["token"]})

        tok = (q.get("t") or [None])[0] or b.get("t")
        with cv:
            u = next(({**v, "email": k} for k, v in db["users"].items() if v["token"] == tok), None)
        if not u: return self.out({"error": "auth"}, 401)
        me, org = u["email"], u["org"]
        same = lambda e: e in db["users"] and db["users"][e]["org"] == org

        if p == "/api/state":
            with cv: seen[me] = time.time(); return self.out(state(u))
        if p == "/api/poll":
            since, end = int((q.get("since") or ["0"])[0]), time.time() + 20
            with cv:
                while True:
                    seen[me] = time.time()
                    ev = [e for e in events if e["id"] > since and me in e["to"]]
                    if ev or time.time() > end: res = {"events": ev, "last": seq}; break
                    cv.wait(5)
            return self.out(res)
        if p == "/api/upload":
            if not raw: return self.out({"error": "empty file"}, 400)
            fid = uuid.uuid4().hex
            with open(os.path.join(FILE_DIR, fid), "wb") as f: f.write(raw)
            meta = {"id": fid, "name": (q.get("name") or ["file"])[0][:200], "type": (q.get("type") or ["application/octet-stream"])[0][:100], "size": len(raw)}
            with cv: db["files"][fid] = {**meta, "org": org}; save()
            return self.out(meta)
        if p.startswith("/files/"):
            m = db["files"].get(p[7:])
            if not m or m["org"] != org: return self.out({"error": "not found"}, 404)
            with open(os.path.join(FILE_DIR, p[7:]), "rb") as f: data = f.read()
            inline = m["type"] in ("image/png", "image/jpeg", "image/gif", "image/webp") and "dl" not in q
            self.send_response(200); self.cors()
            self.send_header("Content-Type", m["type"] if inline else "application/octet-stream")
            self.send_header("Content-Disposition", ("inline" if inline else "attachment") + "; filename*=UTF-8''" + quote(m["name"]))
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data); return

        if p == "/api/groups":
            name = str(b.get("name", "")).strip()[:60]
            members = list(dict.fromkeys([me] + [m for m in b.get("members", []) if same(m)]))
            if not name or len(members) < 2: return self.out({"error": "Name and at least one other member required"}, 400)
            g = {"id": uuid.uuid4().hex[:10], "name": name, "type": "group", "org": org, "members": members, "msgs": []}
            sysmsg(g, f"{u['name']} created the group")
            with cv: db["groups"][g["id"]] = g; save(); emit(members, {"type": "sync"})
            return self.out({"id": g["id"]})
        if p == "/api/dm":
            t = b.get("email")
            if not same(t) or t == me: return self.out({"error": "User not in your organisation"}, 400)
            gid = "d" + hashlib.md5("|".join(sorted([me, t])).encode()).hexdigest()[:10]
            with cv:
                new = gid not in db["groups"]
                g = db["groups"].setdefault(gid, {"id": gid, "name": "", "type": "dm", "org": org, "members": sorted([me, t]), "msgs": []})
                if new: save(); emit(g["members"], {"type": "sync"})
            return self.out({"id": gid})

        g = db["groups"].get(b.get("gid"))
        if not g or me not in g["members"]: return self.out({"error": "no such chat"}, 404)
        if p == "/api/msg":
            text, f = str(b.get("text", ""))[:4000], b.get("file")
            m = {"id": uuid.uuid4().hex[:8], "from": me, "ts": now_ms()}
            if text.strip(): m["text"] = text
            fm = db["files"].get((f or {}).get("id"))
            if fm and fm["org"] == org: m["file"] = {k: fm[k] for k in ("id", "name", "type", "size")}
            if "text" not in m and "file" not in m: return self.out({"error": "empty"}, 400)
            with cv: g["msgs"].append(m); save(); emit(g["members"], {"type": "sync"})
            return self.out({"ok": True})
        if p == "/api/sig":      # WebRTC signalling relay
            ev = {k: v for k, v in b.items() if k != "t"}; ev.update(type="sig", **{"from": me})
            to = [b["to"]] if b.get("to") in g["members"] else [m for m in g["members"] if m != me]
            emit(to, ev); return self.out({"ok": True})
        if p == "/api/cs":       # call join / leave
            with cv:
                live_calls(); c = calls.get(g["id"])
                if b.get("on"):
                    if not c:
                        c = calls[g["id"]] = {"mode": "video" if b.get("mode") == "video" else "audio", "set": []}
                        sysmsg(g, f"{u['name']} started a {c['mode']} call")
                    if me not in c["set"]: c["set"].append(me)
                elif c and me in c["set"]:
                    c["set"].remove(me)
                    if not c["set"]: del calls[g["id"]]; sysmsg(g, "Call ended")
                save(); emit(g["members"], {"type": "sync"})
            return self.out({"ok": True})
        self.out({"error": "unknown route"}, 404)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print("Teams backend running on port", port)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
