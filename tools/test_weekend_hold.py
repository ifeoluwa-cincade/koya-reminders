"""discord_reminder.yml weekend hold (Ife, 23 Nov 2026): no follow-up pings to Hot Leads on Saturday or Sunday, Lagos time.
Runs the workflow's Post step with fake network and clock. Run: python3 tools/test_weekend_hold.py
"""
import yaml, os, sys, io, json, time, urllib.request, contextlib
wf = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", "discord_reminder.yml")))
step = [s for s in wf['jobs']['post']['steps'] if s.get('name','').startswith('Post to Discord')][0]
src = step['run'].split("<<'PY'\n",1)[1].rsplit("\nPY",1)[0]
def run(now_utc, hq, label='reminder', test='false', channel=''):
    calls = []
    class R:
        def __init__(s, b, st=200): s.b=b; s.status=st
        def read(s): return s.b
    def fake(req, timeout=0):
        u = req if isinstance(req, str) else req.full_url
        calls.append(u.split('?')[0])
        if 'fuhold' in u:
            if hq == 'down': raise OSError('down')
            return R(json.dumps({'held': hq}).encode())
        if 'fuping' in u: return R(b'{"skip": false}')
        return R(b'{}')
    os.environ.update({'LIVE_URL':'https://discord.test/live','TEST_URL':'https://discord.test/test','SYSTEM_URL':'https://discord.test/sys','LEAD_ACTIONS_URL':'https://la.test/x','LEAD_ACTIONS_HOOK':'h','MESSAGE':'Quick reminder: X is marked as **Interested**.','LABEL':label,'LEAD':'abc','TEST':test,'CHANNEL':channel})
    urllib.request.urlopen = fake; t0 = time.time; time.time = lambda: now_utc
    code = 0
    try:
        with contextlib.redirect_stdout(io.StringIO()): exec(compile(src,'wf','exec'), {})
    except SystemExit as e: code = e.code if isinstance(e.code,int) else 1
    finally: time.time = t0
    return code, any('discord.test/live' in c for c in calls), calls
import calendar
sat = calendar.timegm((2026,11,21,10,0,0)); sun_late = calendar.timegm((2026,11,22,22,30,0)); fri_late = calendar.timegm((2026,11,20,22,30,0)); mon = calendar.timegm((2026,11,23,9,0,0))
c,p,_ = run(sat, True);  assert (c,p)==(0,False), ('sat held',c,p)
c,p,_ = run(sun_late, True); assert (c,p)==(0,False)
c,p,_ = run(sat, 'down'); assert (c,p)==(1,False), ('sat hq down',c,p)
c,p,_ = run(mon, False); assert (c,p)==(0,True), ('mon',c,p)
c,p,_ = run(fri_late, 'down'); assert (c,p)==(0,True), ('weekday hq down posts as before',c,p)
c,p,calls = run(sat, True, test='true'); assert not any('fuhold' in x for x in calls), 'tests never held'
c,p,calls = run(sat, True, channel='system'); assert not any('fuhold' in x for x in calls), 'system alerts never held'
print('workflow checks: 7 passed')
