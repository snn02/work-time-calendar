"""Transport-independent fact validation, calendar reconciliation and accounting."""
import hashlib
import html
import json
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from history import parse_time, redact

CALENDAR_ID = os.environ.get('WORK_TIME_CALENDAR_ID')
_zone = os.environ.get('WORK_TIME_TIMEZONE', 'Europe/Moscow')
try:
    MOSCOW = ZoneInfo(_zone)
except ZoneInfoNotFoundError:
    if _zone != 'Europe/Moscow':
        raise ValueError('Timezone database unavailable; install tzdata or set WORK_TIME_TIMEZONE to a valid IANA name')
    MOSCOW = timezone(timedelta(hours=3), 'Europe/Moscow')
CATEGORIES = ('продажи','проекты','разработка','встреча внутренняя','встреча с клиентом','общение','отдых','простой')
MARKER = '[work-time-calendar]'
KINDS = ('Агент','Встреча','Работа','Отдых','Простой')
SOURCE_NAMES = {'codex':'Codex','claude':'Claude','opencode':'OpenCode'}


def validate_entry(entry):
    e = dict(entry)
    start, end = parse_time(e.get('start')).replace(microsecond=0), parse_time(e.get('end')).replace(microsecond=0)
    if end <= start:
        raise ValueError('end must be after start')
    for name in ('category','project','action'):
        if not isinstance(e.get(name),str) or not e[name].strip():
            raise ValueError('missing '+name)
        e[name]=e[name].strip()
    if e['category'] not in CATEGORIES:
        raise ValueError('unknown category')
    if e.get('basis') not in ('user','chat_estimate','confirmed_estimate'):
        raise ValueError('basis must be user, chat_estimate or confirmed_estimate')
    sources=e.get('sources',[])
    if not isinstance(sources,list) or any(not isinstance(s,str) or not s for s in sources):
        raise ValueError('sources must be a list of nonempty source IDs')
    if e['basis']=='chat_estimate' and not sources:
        raise ValueError('chat estimate needs source references')
    for key in ('calendar_matches','distinct_event_ids'):
        if not isinstance(e.get(key,[]),list) or any(not isinstance(i,str) for i in e.get(key,[])):
            raise ValueError('invalid '+key)
    e.update(start=start.isoformat(),end=end.isoformat(),sources=sorted(set(sources)))
    default_kind=('Отдых' if e['category']=='отдых' else 'Простой' if e['category']=='простой'
                  else 'Встреча' if e['category'].startswith('встреча') else 'Агент' if sources else 'Работа')
    e['kind']=e.get('kind') or default_kind
    if e['kind'] not in KINDS: raise ValueError('unknown activity kind')
    labels=e.get('source_labels') or sorted({SOURCE_NAMES.get(s.split(':',1)[0],s.split(':',1)[0]) for s in sources})
    e['source_labels']=labels or ['Пользователь']
    e['description']=redact(e.get('description',''))
    identity = [e['start'],e['end'],e['project'],e['sources'] or e['action'].casefold()]
    e['key']=hashlib.sha256(json.dumps(identity,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return e


def to_event(entry):
    e=validate_entry(entry)
    meta={'version':2,'key':e['key'],'project':e['project'],'category':e['category'],'kind':e['kind'],
          'sources':e['sources'],'source_labels':e['source_labels'],'basis':e['basis'],'description_origin':e.get('description_origin','agent')}
    encoded=json.dumps(meta,ensure_ascii=False,separators=(',',':'))
    if len(encoded.encode('utf-8'))>8000:
        raise ValueError('too many source references; split the fact')
    desc=e['description'] or e['action'].rstrip('.!?')+'.'
    detail=e['project'] if e['kind']=='Агент' else e.get('title_detail') or e['action']
    display=f"Описание: {desc}\nПроект: {e['project']}\nКатегория: {e['category'].capitalize()}\nИсточник: {', '.join(e['source_labels'])}"
    return {'id':'tc'+e['key'],'summary':redact(f"{e['kind']}: {detail}"),
            'description':display,
            'start':{'dateTime':e['start']},'end':{'dateTime':e['end']},
            'extendedProperties':{'private':{'wt_key':e['key'],'wt_meta':encoded}}, 'reminders':{'useDefault':False}}


def connector_description(event):
    """Calendar app renders Markdown: blank lines keep fields in paragraphs."""
    return '\n\n'.join(event['description'].splitlines())


def description_text(value):
    # The app returns HTML on write and wrapped plain text on read.
    value=re.sub(r'(?i)</p\s*>|<br\s*/?>|</div\s*>','\n',value or '')
    text=html.unescape(re.sub(r'<[^>]+>', ' ', value))
    return re.sub(r'(?<=\w)-\r?\n(?=\w)', '-', text)


def metadata(event):
    description=description_text(event.get('description'))
    if MARKER in description:
        try:
            block=description.split(MARKER,1)[1].split('[/work-time-calendar]',1)[0].strip()
            return json.loads(block.replace('\r',' ').replace('\n',' '))
        except (ValueError,IndexError):
            return {}
    labels=list(re.finditer(r'(?<!\S)(Проект|Категория|Источник):\s*',description))
    values={m.group(1):' '.join(description[m.end():labels[i+1].start() if i+1<len(labels) else len(description)].split()) for i,m in enumerate(labels)}
    if not all(k in values for k in ('Проект','Категория','Источник')): return {}
    return {'version':2,'project':values['Проект'],'category':values['Категория'].casefold(),
            'source_labels':[x.strip() for x in values['Источник'].split(',') if x.strip()],
            'kind':event.get('summary','').split(':',1)[0]}


def identity_metadata(event):
    """Optional local/hidden identity; it never appears in the readable description."""
    external=event.get('_work_time_identity')
    if isinstance(external,dict): return external
    raw=(event.get('extendedProperties') or {}).get('private',{}).get('wt_meta')
    if raw:
        try: return json.loads(raw)
        except (ValueError,TypeError): pass
    return metadata(event)


def human_description(event):
    value=description_text(event.get('description')).split(MARKER,1)[0]
    value=re.split(r'(?<!\S)Проект:',value,maxsplit=1)[0]
    return ' '.join(re.sub(r'^\s*Описание:\s*','',value).split())


def interval(event):
    try:
        a,b=event['start'],event['end']
        return (parse_time(a if isinstance(a,str) else a['dateTime']).replace(microsecond=0),
                parse_time(b if isinstance(b,str) else b['dateTime']).replace(microsecond=0))
    except (KeyError,TypeError,ValueError):
        return None


def subtract(start,end,covered):
    pieces=[(start,end)]
    for a,b in sorted(covered):
        remaining=[]
        for x,y in pieces:
            if b<=x or a>=y:
                remaining.append((x,y))
            else:
                if x<a: remaining.append((x,a))
                if b<y: remaining.append((b,y))
        pieces=remaining
    return pieces


def reconcile(entries,events):
    result={'create':[],'already':[],'review':[]}
    working=list(events)
    for raw in entries:
        e=validate_entry(raw)
        start,end=parse_time(e['start']),parse_time(e['end'])
        covered=[]
        ambiguous=[]
        known_ids={x.get('id') for x in working}
        unknown=set(e.get('calendar_matches',[]))-known_ids
        if unknown:
            result['review'].append({'entry':e,'reason':'matched event not in current snapshot','event_ids':sorted(unknown)})
            continue
        for event in working:
            if event.get('status')=='cancelled': continue
            span=interval(event)
            if not span: continue
            a,b=span
            if b<=start or a>=end: continue
            m=identity_metadata(event)
            same_source=bool(set(e['sources']) & set(m.get('sources',[]))) and m.get('project')==e['project']
            same_key=m.get('key')==e['key'] or (event.get('extendedProperties',{}).get('private',{}).get('wt_key')==e['key'])
            expected=to_event(e)
            same_display=(not m.get('sources') and metadata(event)==metadata(expected)
                          and event.get('summary')==expected['summary']
                          and human_description(event)==human_description(expected))
            manual_match=event.get('id') in e.get('calendar_matches',[])
            if same_key or same_source or same_display or manual_match:
                covered.append((a,b))
            elif (not m or (m.get('project')==e['project'] and not m.get('sources'))) and event.get('id') not in e.get('distinct_event_ids',[]):
                ambiguous.append({'id':event.get('id'),'summary':event.get('summary',''),'start':a.isoformat(),'end':b.isoformat()})
        # Known source coverage resolves the fact even with unrelated manual overlaps.
        pieces=subtract(start,end,covered)
        if not pieces:
            result['already'].append({'key':e['key'],'project':e['project']})
            continue
        if ambiguous:
            result['review'].append({'entry':e,'reason':'classify overlapping unmarked events as same fact or independent work','events':ambiguous})
            continue
        if covered:
            result['already'].append({'key':e['key'],'project':e['project'],'partial':True})
        for a,b in pieces:
            piece=validate_entry(dict(e,start=a.isoformat(),end=b.isoformat()))
            result['create'].append(piece)
            working.append(to_event(piece))
    result['events']=[to_event(e) for e in result['create']]
    return result


def verify_event(expected,observed):
    if interval(expected)!=interval(observed) or expected.get('summary')!=observed.get('summary'):
        raise ValueError('calendar readback differs in title or interval')
    if human_description(expected)!=human_description(observed) or metadata(expected)!=metadata(observed):
        raise ValueError('calendar readback differs in description or metadata')
    return True


def report(events,start,end):
    if end<=start: raise ValueError('invalid report range')
    spans=[]; projects=defaultdict(float); categories=defaultdict(float); days=defaultdict(float)
    untimed=unknown=estimated=0
    count=0
    for event in events:
        if event.get('status')=='cancelled': continue
        span=interval(event)
        if not span:
            untimed+=1; continue
        a,b=max(start,span[0]),min(end,span[1])
        if b<=a: continue
        m=identity_metadata(event); count+=1
        minutes=(b-a).total_seconds()/60
        project=m.get('project') or 'не классифицировано'
        category=m.get('category') or 'не классифицировано'
        projects[project]+=minutes; categories[category]+=minutes
        if not m: unknown+=1
        if m.get('basis')=='chat_estimate': estimated+=1
        spans.append((a,b))
        cursor=a.astimezone(MOSCOW)
        stop=b.astimezone(MOSCOW)
        while cursor<stop:
            boundary=cursor.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
            tail=min(boundary,stop)
            days[cursor.date().isoformat()]+=(tail-cursor).total_seconds()/60
            cursor=tail
    union=[]
    for a,b in sorted(spans):
        if union and a<=union[-1][1]: union[-1]=(union[-1][0],max(b,union[-1][1]))
        else: union.append((a,b))
    total=sum(projects.values()); occupied=sum((b-a).total_seconds()/60 for a,b in union)
    return {'event_count':count,'project_minutes':round(total,3),'occupied_minutes':round(occupied,3),
            'overlap_minutes':round(total-occupied,3),'by_project':dict(projects),'by_category':dict(categories),
            'by_day':dict(days),'untimed_count':untimed,'unclassified_count':unknown,'estimated_count':estimated,
            'coverage_note':'Calendar contains recorded facts only; blank time is unknown, not idle/rest. Long events do not prove continuous focus.'}
