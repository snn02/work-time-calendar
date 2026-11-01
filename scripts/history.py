"""Read local agent histories. No writes to agent storage, no tool output export."""
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def parse_time(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / (1000 if value > 100000000000 else 1), timezone.utc)
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timestamp needs offset')
    return result


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return '\n'.join(x.get('text', '') for x in value if isinstance(x, dict) and x.get('type') in ('text', 'input_text', 'output_text'))
    return ''


def redact(text):
    text = re.sub(r'(?i)\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}|ya29\.[A-Za-z0-9_-]+|gh[pousr]_[A-Za-z0-9_]{15,})', '[REDACTED]', text)
    return re.sub(r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client_secret|password)["\']?\s*[:=]\s*)["\']?[^\s,"\'}]+', r'\1[REDACTED]', text)


def _system_text(text):
    return text.startswith(('# AGENTS.md instructions for ', '<environment_context>', '<external_codex_apps_open_page>', '<turn_aborted>'))


def _message(agent, sid, role, stamp, text, mid=None):
    # Timestamp+content identities survive duplicated representations and forks.
    stamp = parse_time(stamp).isoformat()
    digest = hashlib.sha256((sid+'\0'+role+'\0'+stamp+'\0'+text).encode()).hexdigest()[:24]
    return {'id': agent+':'+str(mid or digest), 'timestamp': stamp, 'role': role,
            'text': redact(text[:6000]), 'truncated': len(text)>6000,
            'source': agent+':'+sid, 'fingerprint': agent+':'+digest}


def _jsonl(path, agent, start, end, errors):
    sid, project, title, excluded = path.stem, '', '', False
    messages = []
    with path.open(encoding='utf-8') as handle:
        for number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except ValueError:
                errors.append({'agent': agent, 'path': str(path), 'line': number, 'reason': 'partial or malformed JSON line'})
                continue
            kind = row.get('type')
            payload = row.get('payload') or {}
            if kind == 'session_meta' and agent == 'codex':
                sid = str(payload.get('id') or payload.get('session_id') or sid)
                project = payload.get('cwd') or project
                src = payload.get('source')
                excluded = isinstance(src, dict) and 'subagent' in src
                excluded = excluded or bool(payload.get('parent_thread_id') or payload.get('agent_path'))
                if excluded:
                    break
            if agent == 'claude':
                if row.get('isSidechain') or 'subagents' in path.parts:
                    excluded = True
                    break
                sid = str(row.get('sessionId') or sid)
                project = row.get('cwd') or project
                title = row.get('customTitle') or title
            stamp = row.get('timestamp')
            if not stamp:
                continue
            try:
                at = parse_time(stamp)
            except ValueError:
                errors.append({'agent': agent, 'path': str(path), 'line': number, 'reason': 'invalid timestamp'})
                continue
            if not start <= at < end:
                continue
            role, text, mid = None, '', None
            if agent == 'codex':
                if kind == 'event_msg' and payload.get('type') in ('user_message', 'agent_message'):
                    role = 'user' if payload['type']=='user_message' else 'assistant'
                    text = text_content(payload.get('message'))
                    mid = payload.get('message_id')
                elif kind == 'response_item' and payload.get('type') == 'message' and payload.get('role') in ('user','assistant'):
                    role, text = payload['role'], text_content(payload.get('content'))
                    # IDs on response items are often absent; semantic fingerprints dedup event_msg.
                    mid = payload.get('id')
            elif kind in ('user', 'assistant'):
                role = kind
                msg = row.get('message') or {}
                text, mid = text_content(msg.get('content')), row.get('uuid')
            if role and text.strip() and not _system_text(text):
                messages.append(_message(agent, sid, role, stamp, text, mid))
    if excluded or not messages:
        return None
    return {'agent': agent, 'session_id': sid, 'source': agent+':'+sid,
            'project': project, 'title': title, 'path': str(path), 'messages': messages}


def _opencode(db, start, end):
    sessions = []
    conn = sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        # Read a consistent WAL-aware snapshot. Filter messages, not session creation.
        query = '''SELECT m.id AS mid, m.session_id, m.time_created, m.data,
                   s.directory, s.title, s.parent_id FROM message m JOIN session s ON s.id=m.session_id
                   WHERE m.time_created>=? AND m.time_created<? ORDER BY m.time_created,m.id'''
        by_id = {}
        for row in conn.execute(query, (int(start.timestamp()*1000), int(end.timestamp()*1000))):
            if row['parent_id']:
                continue
            data = json.loads(row['data'])
            role = data.get('role')
            if role not in ('user','assistant'):
                continue
            parts = [json.loads(p[0]) for p in conn.execute('SELECT data FROM part WHERE message_id=? ORDER BY time_created,id', (row['mid'],))]
            text = text_content([p for p in parts if not p.get('synthetic') and not p.get('ignored')])
            if not text.strip() or _system_text(text):
                continue
            sid = row['session_id']
            session = by_id.setdefault(sid, {'agent':'opencode','session_id':sid,'source':'opencode:'+sid,'project':row['directory'] or '', 'title':row['title'] or '', 'path':str(db),'messages':[]})
            session['messages'].append(_message('opencode',sid,role,row['time_created'],text,row['mid']))
        sessions.extend(by_id.values())
    finally:
        conn.close()
    return sessions


def collect(start, end, home=None, roots=None):
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError('expected increasing timezone-aware range')
    explicit_home = home is not None
    home = Path(home) if explicit_home else Path.home()
    roots = roots or {}
    codex = Path(roots.get('codex') or (None if explicit_home else os.environ.get('CODEX_HOME')) or home/'.codex')
    claude = Path(roots.get('claude') or (None if explicit_home else os.environ.get('CLAUDE_CONFIG_DIR')) or home/'.claude')
    opencode = Path(roots.get('opencode') or (None if explicit_home else os.environ.get('OPENCODE_DATA_DIR')) or home/'.local/share/opencode')
    errors, coverage, sessions, seen_id, seen_fp = [], [], [], set(), set()
    for agent, folders in [('codex',[codex/'sessions',codex/'archived_sessions']),('claude',[claude/'projects'])]:
        before = len(errors)
        count = 0
        existing = [p for p in folders if p.is_dir()]
        for folder in existing:
            def walk_error(exc):
                errors.append({'agent':agent,'path':str(exc.filename),'reason':str(exc)})
            for directory, dirs, files in os.walk(folder, onerror=walk_error):
                dirs[:] = [d for d in dirs if d != 'subagents']
                for name in sorted(files):
                    if not name.endswith('.jsonl'):
                        continue
                    path = Path(directory)/name
                    count += 1
                    try:
                        s = _jsonl(path,agent,start,end,errors)
                        if s:
                            unique = []
                            for m in s['messages']:
                                if m['id'] not in seen_id and m['fingerprint'] not in seen_fp:
                                    seen_id.add(m['id']); seen_fp.add(m['fingerprint']); unique.append(m)
                            if unique:
                                s['messages'] = sorted(unique,key=lambda m:m['timestamp'])
                                sessions.append(s)
                    except (OSError, ValueError, TypeError, AttributeError) as exc:
                        errors.append({'agent':agent,'path':str(path),'reason':str(exc)})
        coverage.append({'agent':agent,'status':'missing' if not existing else ('partial' if len(errors)>before else 'ok'), 'files_scanned':count})
    db = opencode/'opencode.db'
    before = len(errors)
    if db.is_file():
        try:
            sessions.extend(_opencode(db,start,end))
        except (sqlite3.Error,OSError,ValueError,KeyError) as exc:
            errors.append({'agent':'opencode','path':str(db),'reason':str(exc)})
    coverage.append({'agent':'opencode','status':'missing' if not db.is_file() else ('partial' if len(errors)>before else 'ok')})
    # Consolidate a session split across multiple physical files.
    merged = {}
    for session in sessions:
        if session['source'] in merged:
            merged[session['source']]['messages'].extend(session['messages'])
        else:
            merged[session['source']] = session
    sessions = list(merged.values())
    for s in sessions:
        s['messages'].sort(key=lambda m:parse_time(m['timestamp']))
        s['first_message'] = s['messages'][0]['timestamp']
        s['last_message'] = s['messages'][-1]['timestamp']
        s['user_messages'] = sum(m['role']=='user' for m in s['messages'])
    return {'start':start.isoformat(),'end':end.isoformat(),'complete':all(x['status']=='ok' for x in coverage),
            'coverage':coverage,'errors':errors,'sessions':sorted(sessions,key=lambda s:s['first_message'])}
