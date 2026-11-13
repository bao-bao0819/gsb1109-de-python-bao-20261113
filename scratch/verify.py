import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from textfsm.parser import TextFSM

HERE = Path(__file__).resolve().parent


def parse(template_path, text, eof=True):
    with open(template_path) as f:
        fsm = TextFSM(f)
    return fsm.header, fsm.ParseText(text, eof=eof)


def trace(template_path, text):
    with open(template_path) as f:
        fsm = TextFSM(f)
    log = []
    for line in text.splitlines():
        state = fsm._cur_state_name
        for idx, rule in enumerate(fsm._cur_state):
            if rule.regex_obj.match(line):
                log.append((line, state, idx, str(rule).strip()))
                fsm._CheckLine(line)
                break
    return log


sample = (HERE / 'field_sample.txt').read_text()
snippet = (HERE / 'snippet.txt').read_text()
old_t = HERE / 'device_old.template'
ren_t = HERE / 'device_rename_only.template'
new_t = HERE / 'device_new.template'

print('=== full field sample ===')
h_old, r_old = parse(old_t, sample)
h_ren, r_ren = parse(ren_t, sample)
h_new, r_new = parse(new_t, sample)
for label, rows in (('OLD', r_old), ('RENAME_ONLY', r_ren), ('NEW', r_new)):
    print('%s (%d rows):' % (label, len(rows)))
    for row in rows:
        print('  ', row)
assert h_old == h_ren == h_new
assert r_old == r_ren
assert len(r_new) - len(r_old) == 2

print()
print('=== earliest output-changing transition (per-line rule hit) ===')
traces = {'OLD': trace(old_t, sample), 'NEW': trace(new_t, sample)}
for i, line in enumerate(sample.splitlines()):
    o = traces['OLD'][i]
    n = traces['NEW'][i]
    flag = ' <-- FIRST DIVERGENCE' if o[2:] != n[2:] and i == 0 else (
        ' <-- divergence' if o[2:] != n[2:] else '')
    print('line %d %r' % (i + 1, line))
    print('   OLD: state=%s rule#%s %s' % (o[1], o[2], o[3]))
    print('   NEW: state=%s rule#%s %s%s' % (n[1], n[2], n[3], flag))

print()
print('=== minimal snippet ===')
s_old = parse(old_t, snippet)[1]
s_ren = parse(ren_t, snippet)[1]
s_new = parse(new_t, snippet)[1]
print('OLD rows        :', s_old)
print('RENAME_ONLY rows:', s_ren)
print('NEW rows        :', s_new)
assert s_old == s_ren == [] and len(s_new) == 2

print()
print('=== EOF independence ===')
print('OLD eof=False:', parse(old_t, snippet, eof=False)[1])
print('NEW eof=False:', parse(new_t, snippet, eof=False)[1])
assert parse(old_t, snippet, eof=False)[1] == s_old
assert parse(new_t, snippet, eof=False)[1] == s_new
for extra in ('', '\n', '\n\n'):
    a = parse(old_t, snippet + extra)[1]
    b = parse(new_t, snippet + extra)[1]
    assert (len(a), len(b)) == (0, 2), extra

with open(new_t) as f:
    fsm = TextFSM(f)
fsm.ParseText(snippet)
assert all(v.value in (None, []) for v in fsm.values)
print('pending record buffer after snippet is empty; rows came from explicit Record rules.')

print()
print('ALL CHECKS PASSED')
