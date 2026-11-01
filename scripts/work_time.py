"""Optional local helper. Google actions are performed by the host's connector."""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from history import collect, parse_time
from time_core import MOSCOW, CALENDAR_ID, reconcile, report, validate_entry, to_event, verify_event


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def date_range(args):
    now=datetime.now(MOSCOW)
    if args.start:
        start=parse_time(args.start)
        end=parse_time(args.end) if args.end else now
    else:
        day=now.date() if args.date=='today' else datetime.strptime(args.date,'%Y-%m-%d').date()
        start=datetime.combine(day,datetime.min.time(),MOSCOW)
        end=min(start+timedelta(days=1),now) if day==now.date() else start+timedelta(days=1)
    if end<=start: raise ValueError('range must end after start')
    return start,end


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('collect','report'):
        p=sub.add_parser(name)
        p.add_argument('--date',default='today',help='today or YYYY-MM-DD (configured timezone)')
        p.add_argument('--from',dest='start',help='ISO timestamp including offset')
        p.add_argument('--to',dest='end',help='ISO timestamp including offset')
        p.add_argument('--output',help='explicit local JSON output path')
        if name=='collect':
            p.add_argument('--home',help='override home for testing/other local profile')
            p.add_argument('--source',action='append',choices=['codex','claude','opencode'],help='optional source restriction; default all three')
            p.add_argument('--session',help='restrict to one known source ID, e.g. codex:UUID')
            p.add_argument('--summary',action='store_true')
        else: p.add_argument('--events',required=True,help='normalized connector event JSON')
    p=sub.add_parser('prepare');p.add_argument('--entries',required=True);p.add_argument('--output')
    p=sub.add_parser('reconcile');p.add_argument('--entries',required=True);p.add_argument('--events',required=True);p.add_argument('--output')
    p=sub.add_parser('verify');p.add_argument('--expected',required=True);p.add_argument('--observed',required=True);p.add_argument('--output')
    p=sub.add_parser('doctor');p.add_argument('--output')
    args=parser.parse_args(argv)
    if args.command=='collect':
        start,end=date_range(args)
        result=collect(start,end,Path(args.home) if args.home else None)
        if args.source:
            result['sessions']=[s for s in result['sessions'] if s['agent'] in args.source]
            result['coverage']=[c for c in result['coverage'] if c['agent'] in args.source]
            result['complete']=all(c['status']=='ok' for c in result['coverage'])
        if args.session:
            result['sessions']=[s for s in result['sessions'] if s['source']==args.session]
            result['requested_session_found']=bool(result['sessions'])
        if args.summary:
            result['sessions']=[{k:v for k,v in s.items() if k not in ('messages','path')} for s in result['sessions']]
    elif args.command=='prepare':
        entries=read_json(args.entries)
        if isinstance(entries,dict): entries=entries['entries']
        result={'calendar_id':CALENDAR_ID,'events':[to_event(validate_entry(e)) for e in entries]}
    elif args.command=='reconcile':
        entries=read_json(args.entries)
        events=read_json(args.events)
        result=reconcile(entries.get('entries',[]) if isinstance(entries,dict) else entries,
                         events.get('items',[]) if isinstance(events,dict) else events)
        result['calendar_id']=CALENDAR_ID
    elif args.command=='report':
        start,end=date_range(args); events=read_json(args.events)
        result=report(events.get('items',[]) if isinstance(events,dict) else events,start,end)
    elif args.command=='verify':
        result={'verified':verify_event(read_json(args.expected),read_json(args.observed))}
    else:
        result={'python':sys.version.split()[0],'calendar_id':CALENDAR_ID,'timezone':str(MOSCOW),
                'calendar_transport':'host Google Calendar connector (not authenticated or probed by this offline helper)',
                'history_home':str(Path.home()),'gws_required':False}
    body=json.dumps(result,ensure_ascii=False,indent=2)
    if getattr(args,'output',None):
        path=Path(args.output).expanduser().resolve()
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(body,encoding='utf-8')
        print(json.dumps({'output':str(path),'bytes':len(body.encode())}))
    else: print(body)
    return 0


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    try: raise SystemExit(main())
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
