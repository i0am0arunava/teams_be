import json, os, time, threading
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024
CORS(app)

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db.json')  # JSON "database"
lock = threading.Lock()
signals = {}   # email -> queued WebRTC/ring messages (memory only)
rooms = {}     # room(gid) -> {email: name}  active calls
seen = {}      # email -> last poll time (presence)

def load():
    try:
        with open(DB) as f: return json.load(f)
    except Exception:
        return {'users': {}, 'groups': {}, 'messages': {}}

def save(d):
    with open(DB, 'w') as f: json.dump(d, f)

J = lambda: request.get_json(force=True, silent=True) or {}

@app.get('/')
def home(): return 'Teams clone server running'

@app.post('/api/login')
def login():
    j = J(); email = (j.get('email') or '').strip().lower(); name = (j.get('name') or '').strip()
    if '@' not in email or not email.split('@')[1] or not name:
        return jsonify(error='Enter a valid name and email'), 400
    with lock:
        d = load()
        d['users'][email] = {'email': email, 'name': name, 'org': email.split('@')[1]}  # org = domain after @
        save(d)
    return jsonify(d['users'][email])

@app.get('/api/poll')
def poll():
    e = request.args.get('email', ''); gid = request.args.get('gid', ''); since = int(request.args.get('since', 0))
    with lock:
        d = load(); me = d['users'].get(e)
        if not me: return jsonify(error='login'), 401
        seen[e] = time.time()
        users = [dict(u, online=time.time() - seen.get(u['email'], 0) < 6) for u in d['users'].values() if u['org'] == me['org']]
        groups = []
        for g in d['groups'].values():
            if e in g['members']:
                m = d['messages'].get(g['id'], []); last = m[-1] if m else None
                groups.append(dict(g, last=(last['text'] or (last['file'] or {}).get('name', '')) if last else '',
                                   ts=last['ts'] if last else g['ts'], call=len(rooms.get(g['id'], {}))))
        msgs = d['messages'].get(gid, [])[since:] if gid and gid in d['groups'] and e in d['groups'][gid]['members'] else []
        sig = signals.pop(e, [])
    return jsonify(users=users, groups=groups, msgs=msgs, signals=sig, gid=gid)

@app.post('/api/group')
def group():
    j = J(); creator = j['email']
    with lock:
        d = load(); members = sorted(set(j['members'] + [creator]))
        if j.get('dm'):
            gid = 'dm:' + '|'.join(members)
            d['groups'].setdefault(gid, {'id': gid, 'name': '', 'dm': True, 'members': members, 'ts': time.time()})
        else:
            gid = 'g' + str(int(time.time() * 1000))
            d['groups'][gid] = {'id': gid, 'name': j['name'], 'dm': False, 'members': members, 'ts': time.time()}
        save(d)
    return jsonify(d['groups'][gid])

@app.post('/api/send')
def send():
    j = J()
    with lock:
        d = load()
        d['messages'].setdefault(j['gid'], []).append({'from': j['email'], 'name': j['name'], 'text': j.get('text', ''),
                                                       'file': j.get('file'), 'ts': time.time()})
        save(d)
    return jsonify(ok=1)

@app.post('/api/call')
def call():
    j = J(); room = j['room']; e = j['email']
    with lock:
        r = rooms.setdefault(room, {})
        if j['act'] == 'join':
            peers = [{'email': p, 'name': n} for p, n in r.items() if p != e]
            r[e] = j.get('name', e)
            for p in j.get('ring', []):
                signals.setdefault(p, []).append({'t': 'ring', 'room': room, 'from': e, 'name': j.get('name'), 'title': j.get('title', '')})
            return jsonify(peers=peers)
        r.pop(e, None)
        for p in r: signals.setdefault(p, []).append({'t': 'left', 'from': e})
        if not r: rooms.pop(room, None)
    return jsonify(ok=1)

@app.post('/api/signal')
def signal():
    j = J()
    with lock:
        signals.setdefault(j['to'], []).append({'t': j['t'], 'from': j['email'], 'name': j.get('name'), 'data': j.get('data')})
    return jsonify(ok=1)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
