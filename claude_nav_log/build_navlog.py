#!/usr/bin/env python3
"""Build data.js for the Claude x robot navigation log page.

Usage:
  python3 build_navlog.py [--hide NAME ...] CLAUDE_TRANSCRIPT.jsonl ROBOT_SESSION_DIR [ROBOT_SESSION_DIR ...]

--hide NAME             : extra host name to replace with ROBOT_HOST (repeatable); tailnet
                          *.ts.net hosts are always replaced

CLAUDE_TRANSCRIPT.jsonl : transcript.jsonl from a Claude Code session export zip
ROBOT_SESSION_DIR       : unzipped agent_nav export (contains log.jsonl, session.json, thumbs/)

Writes data.js, log.md (plain-text timeline) and thumbs/ next to this script. Everything that ends up in the page is passed
through redact() first; review the output before publishing anyway.
"""
import base64, json, os, re, shutil, sys
from datetime import datetime
from math import cos, sin

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_OUT = 6000  # chars of tool output kept per call
FULL = {}       # thumbnail file -> full-resolution copy (images Claude looked at, from the transcript)

REDACTIONS = [
    (re.compile(r'[A-Za-z0-9-]+\.tail[0-9a-f]+\.ts\.net'), 'ROBOT_HOST'),    # tailnet hosts
    (re.compile(r'tail[0-9a-f]{4,}'), 'tailnet'),
    (re.compile(r'/(?:private/)?tmp/claude-\d+/[^\s/]+/[^\s/]+/scratchpad'), '$TMP'),
    (re.compile(r'/(?:private/)?tmp/claude-\d+/[^\s/]+'), '$TMP'),
    (re.compile(r'/Users/[^/\s]+'), '~'),
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+'), '<email>'),
    (re.compile(r'(?i)((?:api[_-]?key|token|secret|password|authorization)["\']?\s*[:=]\s*["\']?)[^\s"\',}]+'), r'\1<redacted>'),
    (re.compile(r'\b(?:sk|pk|ghp|gho|wandb|hf)_[A-Za-z0-9_-]{16,}\b'), '<redacted>'),
    (re.compile(r'\bsk-[A-Za-z0-9_-]{16,}\b'), '<redacted>'),
]
IMG_REF = re.compile(r'\[Image: source: [^\]]*\]')
HARNESS_TAGS =re.compile(r'<(system-reminder|command-[\w-]+|local-command-[\w-]+|ide_[\w-]+|pasted_content[^>]*)>.*?</\1\s*>', re.S)


def add_hidden_names(names):
    """Extra host names to hide (from --hide NAME), e.g. a short MagicDNS name."""
    for n in names:
        REDACTIONS.insert(0, (re.compile(r'(?i)\b' + re.escape(n) + r'(?:[\w.-]*)'), 'ROBOT_HOST'))


def redact(s):
    if not isinstance(s, str):
        return s
    for pat, rep in REDACTIONS:
        s = pat.sub(rep, s)
    return s


def redact_obj(o):
    if isinstance(o, dict):
        return {k: redact_obj(v) for k, v in o.items()}
    if isinstance(o, list):
        return [redact_obj(v) for v in o]
    return redact(o)


def ts(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()


def clip(s, n=MAX_OUT):
    return s if len(s) <= n else s[:n] + f'\n… [{len(s) - n} more characters]'


# ---------------------------------------------------------------- robot side
def load_robot(dirs):
    rows, sessions, thumbs_by_stem = [], [], {}
    os.makedirs(os.path.join(HERE, 'thumbs'), exist_ok=True)
    for si, d in enumerate(dirs):
        meta = json.load(open(os.path.join(d, 'session.json')))
        sessions.append({'id': meta['id'], 'started': meta['started'], 'ended': meta.get('ended'),
                         'profile': meta.get('profile'), 'index': si})
        snap_pose = {}
        for line in open(os.path.join(d, 'log.jsonl')):
            r = json.loads(line)
            rep, req = r.get('reply') or {}, r.get('request') or {}
            if r['kind'] == 'look' and 'pose_world' in rep:
                snap_pose[rep['snapshot']] = rep['pose_world']
            for t in r.get('thumbs', []):
                shutil.copy(os.path.join(d, 'thumbs', t), os.path.join(HERE, 'thumbs', t))
                stem = re.sub(r'^\d+_', '', t).rsplit('-', 1)[0]      # 00085_s14-68.jpg -> s14
                thumbs_by_stem[stem] = t
            row = {'n': r['n'], 'session': si, 't': r['wall'], 'actor': r['actor'], 'kind': r['kind'],
                   'comment': r.get('comment', ''), 'level': r.get('level'), 'thumbs': r.get('thumbs', []),
                   'request': req, 'reply': rep}
            geo = {}
            if r['kind'] == 'look':
                geo['pose'] = rep.get('pose_world')
            elif r['kind'] == 'event' and rep.get('pose_world'):
                geo['pose'] = rep['pose_world']
            elif r['kind'] in ('goal', 'plan_check') and 'goal' in rep:
                geo['from'] = rep['goal'].get('from_world')
                geo['target'] = rep['goal'].get('world')
            elif r['kind'] == 'query_region' and 'world' in rep:
                w = rep['world']
                geo['point'] = w.get('centroid')
                geo['extent'] = [[w['x'][0], w['y'][0], w['z'][0]], [w['x'][2], w['y'][2], w['z'][2]]]
            elif r['kind'] == 'query_plane' and 'world' in rep:
                geo['point'] = rep['world'].get('centre')
                pose = snap_pose.get(rep.get('snapshot'))
                if pose and rep.get('corners_base'):
                    x0, y0, th = pose
                    geo['corners'] = [[round(x0 + cos(th) * bx - sin(th) * by, 4),
                                       round(y0 + sin(th) * bx + cos(th) * by, 4), round(bz, 4)]
                                      for bx, by, bz in rep['corners_base']]
            elif r['kind'] == 'place':
                geo['place'] = [rep.get('x'), rep.get('y')]
            row['geo'] = geo
            rows.append(row)
    rows.sort(key=lambda r: r['t'])
    return sessions, rows, thumbs_by_stem


# ---------------------------------------------------------------- claude side
def text_of(content):
    if isinstance(content, str):
        return content, []
    out, imgs = [], []
    for c in content or []:
        if c.get('type') == 'text':
            out.append(c.get('text', ''))
        elif c.get('type') == 'image':
            imgs.append(c)
    return '\n'.join(out), imgs


def load_claude(path, thumbs_by_stem):
    rows = [json.loads(l) for l in open(path)]
    rows = [r for r in rows if r.get('type') in ('user', 'assistant') and not r.get('isSidechain')
            and isinstance(r.get('message'), dict)]
    rows.sort(key=lambda r: r['timestamp'])
    steps, by_tool = [], {}
    for r in rows:
        t = ts(r['timestamp'])
        content = r['message'].get('content')
        if r['type'] == 'user':
            if isinstance(content, str):
                content = [{'type': 'text', 'text': content}]
            for b in content or []:
                if b.get('type') == 'tool_result':
                    st = by_tool.get(b.get('tool_use_id'))
                    if not st:
                        continue
                    txt, imgs = text_of(b.get('content'))
                    st['t1'] = t
                    st['output'] = clip(redact(txt))
                    st['is_error'] = bool(b.get('is_error'))
                    if imgs and st.get('image_stem'):
                        st['image'] = thumbs_by_stem.get(st['image_stem'])
                        src = imgs[0].get('source') or {}
                        if src.get('type') == 'base64' and src.get('data'):
                            ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}.get(src.get('media_type'), 'jpg')
                            os.makedirs(os.path.join(HERE, 'full'), exist_ok=True)
                            name = f"full/{st['image_stem']}.{ext}"
                            with open(os.path.join(HERE, name), 'wb') as fh:
                                fh.write(base64.b64decode(src['data']))
                            st['full'] = name
                            if st['image']:
                                FULL[st['image']] = name
                elif b.get('type') == 'text':
                    txt = HARNESS_TAGS.sub('', b.get('text', ''))
                    txt = IMG_REF.sub('', txt).strip()   # "[Image: source: …]" path refs; the image itself is shown
                    if txt and not txt.startswith('<'):
                        steps.append({'type': 'user', 't0': t, 't1': t, 'text': redact(txt)})
                elif b.get('type') == 'image':
                    src = b.get('source') or {}
                    st = {'type': 'user', 't0': t, 't1': t, 'text': ''}
                    if src.get('type') == 'base64' and src.get('data'):
                        ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}.get(src.get('media_type'), 'jpg')
                        os.makedirs(os.path.join(HERE, 'full'), exist_ok=True)
                        name = f'full/user_{sum(1 for s in steps if s.get("user_image")) + 1}.{ext}'
                        with open(os.path.join(HERE, name), 'wb') as fh:
                            fh.write(base64.b64decode(src['data']))
                        st['user_image'] = name
                    else:
                        st['text'] = '[image attached]'
                    steps.append(st)
            continue
        for b in content or []:
            bt = b.get('type')
            if bt == 'thinking' or bt == 'redacted_thinking':
                if steps and steps[-1]['type'] == 'thinking':
                    steps[-1]['count'] += 1
                else:
                    steps.append({'type': 'thinking', 't0': t, 't1': t, 'count': 1})
            elif bt == 'text' and b.get('text', '').strip():
                steps.append({'type': 'text', 't0': t, 't1': t, 'text': redact(b['text'])})
            elif bt == 'tool_use':
                name, inp = b['name'], b.get('input', {})
                st = {'type': 'tool', 't0': t, 't1': t, 'name': name}
                if name == 'Bash':
                    st['title'] = redact(inp.get('description') or '')
                    st['command'] = redact(inp.get('command', ''))
                    st['background'] = bool(inp.get('run_in_background'))
                elif name == 'Read':
                    p = redact(inp.get('file_path', ''))
                    st['title'] = 'Look at ' + os.path.basename(p)
                    st['command'] = p
                    st['image_stem'] = os.path.splitext(os.path.basename(p))[0]
                elif name.endswith('update_plan'):
                    st['name'] = 'plan'
                    st['plan'] = redact_obj(inp)
                elif name.endswith('wrap_up'):
                    st['name'] = 'wrap_up'
                    st['plan'] = redact_obj({k: v for k, v in inp.items() if k in ('recap', 'ask')})
                else:
                    st['title'] = name
                    st['command'] = redact(json.dumps(inp, indent=1))
                steps.append(st)
                by_tool[b['id']] = st
    for i, s in enumerate(steps):
        s['i'] = i
        s.pop('image_stem', None)
    return steps


def link(steps, rows):
    """Attach each robot row to the Claude step that caused it (by wall-clock time)."""
    calls = [s for s in steps if s['type'] == 'tool' and s['name'] == 'Bash']
    for r in rows:
        inside = [s for s in calls if s['t0'] - 0.5 <= r['t'] <= s['t1'] + 0.5 and not s.get('background')]
        if not inside:
            inside = [s for s in calls if s['t0'] - 0.5 <= r['t'] <= s['t1'] + 0.5]
        if inside:
            r['step'] = inside[-1]['i']
        else:
            before = [s for s in steps if s['t0'] <= r['t']]
            r['step'] = before[-1]['i'] if before else 0
    for s in steps:
        s['robot'] = [r['n'] for r in rows if r['step'] == s['i']]


ENDPOINT = {'session': 'POST /session', 'enable': 'POST /enable', 'look': 'POST /look',
            'query_region': 'POST /query/region', 'query_plane': 'POST /query/plane',
            'plan_check': 'POST /plan/check', 'plan_approach': 'POST /plan/approach', 'goal': 'POST /goal',
            'event': 'robot event', 'note': 'POST /memory/note', 'place': 'POST /memory/place',
            'disable': 'POST /disable', 'session_end': 'DELETE /session', 'stop': 'POST /stop'}


def f2(v):
    return f'{v:.2f}' if isinstance(v, (int, float)) else str(v)


def xyz(a):
    return '(' + ', '.join(f2(v) for v in a[:3]) + ')' if a else ''


def summarize(r):
    q, p = r['request'] or {}, r['reply'] or {}
    k = r['kind']
    if k == 'look':
        return f"snapshot {p.get('snapshot')}, robot pose (x, y, θ) {xyz(p.get('pose_world'))}"
    if k == 'query_region':
        return (f"{p.get('query')} on {p.get('snapshot')}, select {json.dumps(q.get('select'))}: "
                f"centroid {xyz((p.get('world') or {}).get('centroid'))}, range {f2((p.get('range_m') or [0, 0])[1])} m")
    if k == 'query_plane':
        return (f"{p.get('query')} on {p.get('snapshot')}, select {json.dumps(q.get('select'))}: {p.get('orientation')} plane "
                f"{f2(p.get('width_m'))}×{f2(p.get('height_m'))} m, centre {xyz((p.get('world') or {}).get('centre'))}, "
                f"inliers {f2(p.get('inlier_fraction'))}")
    if k in ('goal', 'plan_check'):
        c = p.get('check') or {}
        s = f"{q.get('frame')} x={f2(q.get('x'))} y={f2(q.get('y'))} θ={f2(q.get('theta'))}"
        s += f" → world {xyz((p.get('goal') or {}).get('world'))}, clearance {f2(c.get('min_clearance_m'))} m"
        return (f"goal {p.get('goal_id')}: " if k == 'goal' else '') + s
    if k == 'event':
        return f"{p.get('message')}, pose {xyz(p.get('pose_world'))}"
    if k == 'note':
        return q.get('text', '')
    if k == 'place':
        return f"{p.get('place')} at ({f2(p.get('x'))}, {f2(p.get('y'))})"
    return ''


def write_markdown(data, path):
    t0 = data['t0']
    mmss = lambda t: f'{int((t - t0) // 60):02d}:{int((t - t0) % 60):02d}'
    robot = {r['n']: r for r in data['robot']}
    out = ['# Claude × robot: navigation and parking log', '',
           'Plain-text version of index.html. One timeline, in order. Times are mm:ss from the start of the Claude session.',
           'Each Claude tool call is followed by the robot API calls it caused ("robot received").',
           'Claude\'s thinking was not exported, so it appears only as "(thought)". World frame: metres, θ in radians.', '',
           'Robot sessions: ' + ', '.join(f"{s['id']} (profile {s['profile']})" for s in data['sessions']), '']
    for s in data['steps']:
        tm = mmss(s['t0'])
        if s['type'] == 'user':
            out += [f'## [{tm}] User', '']
            if s.get('user_image'):
                out += [f"Image attached: [{s['user_image']}]({s['user_image']})", '']
            if s['text']:
                out += ['> ' + s['text'].replace('\n', '\n> '), '']
        elif s['type'] == 'text':
            out += [f'## [{tm}] Claude', '', s['text'], '']
        elif s['type'] == 'thinking':
            out += [f'_[{tm}] (thought)_', '']
        elif s['name'] == 'plan':
            items = (s.get('plan') or {}).get('items')
            if items:
                mark = {'completed': 'x', 'in_progress': '>', 'pending': ' '}
                out += [f'### [{tm}] Claude plan', ''] + [f"- [{mark.get(i['status'], ' ')}] {i['content']}" for i in items] + ['']
        elif s['name'] == 'wrap_up':
            out += [f'### [{tm}] Claude wrap-up', '', (s.get('plan') or {}).get('recap', ''), '']
        else:
            out += [f"### [{tm}] {s['name']}: {s.get('title') or ''}", '', '```bash' if s['name'] == 'Bash' else '```',
                    clip(s['command'], 2000), '```', '']
            if s.get('output'):
                out += ['Output:', '', '```', clip(s['output'], 1500), '```', '']
            if s.get('image'):
                out += [f"Image: [thumbs/{s['image']}](thumbs/{s['image']})", '']
            if s['robot']:
                out += ['Robot received:', '']
                for n in s['robot']:
                    r = robot[n]
                    line = f"- `{mmss(r['t'])}` **{ENDPOINT.get(r['kind'], r['kind'])}**"
                    if r['comment']:
                        line += f" “{r['comment']}”"
                    sm = summarize(r)
                    if sm:
                        line += f' · {sm}'
                    for w in (r['reply'] or {}).get('warnings', []):
                        line += f'\n  - warning: {w}'
                    out.append(line)
                out.append('')
    with open(path, 'w') as f:
        f.write('\n'.join(out))


def main():
    args, hide = [], []
    it = iter(sys.argv[1:])
    for a in it:
        if a == '--hide':
            hide.append(next(it))
        else:
            args.append(a)
    if len(args) < 2:
        sys.exit(__doc__)
    add_hidden_names(hide)
    sessions, rows, stems = load_robot(args[1:])
    steps = load_claude(args[0], stems)
    link(steps, rows)
    rows = redact_obj(rows)
    data = {'sessions': sessions, 'steps': steps, 'robot': rows, 'full': FULL,
            't0': min(steps[0]['t0'], rows[0]['t']), 't1': max(steps[-1]['t1'], rows[-1]['t'])}
    with open(os.path.join(HERE, 'data.js'), 'w') as f:
        f.write('window.NAVLOG = ')
        json.dump(data, f, separators=(',', ':'))
        f.write(';\n')
    write_markdown(data, os.path.join(HERE, 'log.md'))
    linked = sum(1 for s in steps if s['robot'])
    print(f'{len(steps)} chat steps, {len(rows)} robot turns, {linked} steps linked to robot turns')


if __name__ == '__main__':
    main()
