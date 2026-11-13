import json, os, time, threading, hashlib
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 8 * 1024 * 1024
CORS(app)

# ---- JSON "database" (all files live in ./data, override with DATA_DIR env) ----
DATA = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
MSG = os.path.join(DATA, 'messages')            # one json file per chat
USERS, ORGS, GROUPS = (os.path.join(DATA, n) for n in ('users.json', 'orgs.json', 'groups.json'))
os.makedirs(MSG, exist_ok=True)
lock = threading.Lock()

def rj(p, default):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return default

def wj(p, d):
    with open(p + '.tmp', 'w') as f: json.dump(d, f)
    os.replace(p + '.tmp', p)

for p in (USERS, ORGS, GROUPS):
    if not os.path.exists(p): wj(p, {})

mpath = lambda gid: os.path.join(MSG, hashlib.md5(gid.encode()).hexdigest() + '.json')

# ---- memory-only state (calls) ----
signals, rooms, seen = {}, {}, {}
J = lambda: request.get_json(force=True, silent=True) or {}

@app.get('/')
def home(): return 'Teams clone server running'

@app.get('/api/health')
def health():
    o = rj(ORGS, {}); return jsonify(users=len(rj(USERS, {})), orgs={k: len(v['members']) for k, v in o.items()})

@app.post('/api/login')
def login():
    j = J(); email = (j.get('email') or '').strip().lower(); name = (j.get('name') or '').strip()
    if email.count('@') != 1 or not email.split('@')[1] or not name:
        return jsonify(error='Enter a valid name and email'), 400
    org = email.split('@')[1]
    with lock:
        users, orgs = rj(USERS, {}), rj(ORGS, {})
        users[email] = {'email': email, 'name': name, 'org': org}
        o = orgs.setdefault(org, {'name': org, 'members': []})
        if email not in o['members']: o['members'].append(email)
        wj(USERS, users); wj(ORGS, orgs)
    return jsonify(users[email])

@app.get('/api/poll')
def poll():
    e = request.args.get('email', '').lower(); gid = request.args.get('gid', ''); since = int(request.args.get('since', 0))
    with lock:
        users, orgs, groups = rj(USERS, {}), rj(ORGS, {}), rj(GROUPS, {})
        me = users.get(e)
        if not me: return jsonify(error='login'), 401
        seen[e] = time.time()
        members = [dict(users[m], online=time.time() - seen.get(m, 0) < 6) for m in orgs.get(me['org'], {}).get('members', []) if m in users]
        mine = [dict(g, call=len(rooms.get(g['id'], {}))) for g in groups.values() if e in g['members']]
        msgs = rj(mpath(gid), [])[since:] if gid and gid in groups and e in groups[gid]['members'] else []
        sig = signals.pop(e, [])
    return jsonify(users=members, groups=mine, msgs=msgs, signals=sig, gid=gid)

@app.post('/api/group')
def group():
    j = J(); creator = j['email'].lower()
    with lock:
        users, groups = rj(USERS, {}), rj(GROUPS, {})
        org = users[creator]['org']
        members = sorted({m.lower() for m in j['members'] if m.lower() in users and users[m.lower()]['org'] == org} | {creator})
        if j.get('dm'):
            gid = 'dm:' + '|'.join(members)
            groups.setdefault(gid, {'id': gid, 'name': '', 'dm': True, 'members': members, 'ts': time.time(), 'last': ''})
        else:
            gid = 'g' + str(int(time.time() * 1000))
            groups[gid] = {'id': gid, 'name': j['name'], 'dm': False, 'members': members, 'ts': time.time(), 'last': ''}
        wj(GROUPS, groups)
    return jsonify(groups[gid])

@app.post('/api/send')
def send():
    j = J(); gid = j['gid']
    with lock:
        groups = rj(GROUPS, {})
        if gid not in groups or j['email'].lower() not in groups[gid]['members']: return jsonify(error='forbidden'), 403
        m = rj(mpath(gid), []); now = time.time()
        m.append({'from': j['email'].lower(), 'name': j['name'], 'text': j.get('text', ''), 'file': j.get('file'), 'ts': now})
        wj(mpath(gid), m)
        groups[gid]['ts'] = now; groups[gid]['last'] = j.get('text') or (j.get('file') or {}).get('name', '')
        wj(GROUPS, groups)
    return jsonify(ok=1)

@app.post('/api/call')
def call():
    j = J(); room = j['room']; e = j['email'].lower()
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
    with lock: signals.setdefault(j['to'], []).append({'t': j['t'], 'from': j['email'].lower(), 'name': j.get('name'), 'data': j.get('data')})
    return jsonify(ok=1)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), threaded=True)
