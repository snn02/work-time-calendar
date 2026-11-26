"""Local CalDAV connector. Reads credential from Windows Credential Manager."""
import argparse,json,re,sys
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
import caldav
from icalendar import Calendar,Event
from win32ctypes.pywin32 import win32cred

class ConnectorError(Exception):pass

# RFC 7986 COLOR uses CSS3 names; approximate Google event colors.
CALDAV_COLORS={'7':'turquoise','3':'plum','6':'sandybrown','10':'limegreen','8':'lightgray','5':'gold'}

def event_color(event):
    cid=event.get('colorId')
    if cid is None:return None
    if cid not in CALDAV_COLORS:raise ValueError('Unknown activity color')
    return CALDAV_COLORS[cid]

def select_profile(config,name):
    p=config.get('connections',{}).get(name)
    if not isinstance(p,dict) or p.get('provider')!='caldav':raise ValueError('CalDAV profile not found')
    return p

def select_calendar(principal,name):
    c=[x for x in principal.calendars() if (x.get_display_name() or '').strip()==name]
    if len(c)!=1:raise ValueError('Calendar name must resolve uniquely')
    return c[0]

def date_time(obj):
    d=datetime.fromisoformat(obj['dateTime'].replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('Time zone is required')
    return d

def event_uid(event):
    value=event.get('id','')
    if not re.fullmatch(r'tc[0-9a-f]{3,128}',value):raise ValueError('Expected stable work-time event ID')
    return value+'@work-time-calendar'

def make_ical(event):
    start,end=date_time(event['start']),date_time(event['end'])
    if end<=start:raise ValueError('End must be later than start')
    cal=Calendar();cal.add('prodid','-//Work Time Calendar//RU');cal.add('version','2.0')
    item=Event();item.add('uid',event_uid(event));item.add('dtstamp',datetime.now(timezone.utc));item.add('dtstart',start.astimezone(timezone.utc));item.add('dtend',end.astimezone(timezone.utc))
    item.add('summary',event['summary']);item.add('description',event['description'])
    color=event_color(event)
    if color:item.add('color',color)
    meta=(event.get('extendedProperties') or {}).get('private',{}).get('wt_meta')
    if meta:item.add('x-work-time-meta',meta)
    cal.add_component(item);return cal.to_ical().decode('utf-8')

def create_calendar_event(calendar,event):
    return calendar.add_event(ical=make_ical(event),no_overwrite=True)

def normalize(obj):
    e=obj.icalendar_component
    def stamp(name):
        d=e.decoded(name,None)
        return d.isoformat() if d is not None else None
    raw=str(e.get('x-work-time-meta',''));m={}
    if raw:
        try:m=json.loads(raw)
        except (ValueError,TypeError):pass
    result={'id':str(e.get('uid','')),'summary':str(e.get('summary','')),'description':str(e.get('description','')),'start':stamp('dtstart'),'end':stamp('dtend'),'status':str(e.get('status','')).lower()}
    if m:result['_work_time_identity']=m
    color=e.get('color')
    if color:result['color']=str(color)
    return result

def open_client(profile):
    url=profile.get('url','');parts=urlsplit(url)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:raise ValueError('Invalid CalDAV URL')
    cred=win32cred.CredRead(profile['credential_target'],win32cred.CRED_TYPE_GENERIC,0)
    blob=cred['CredentialBlob'];pw=blob.decode('utf-16-le').rstrip('\x00') if isinstance(blob,bytes) else blob
    return caldav.DAVClient(url=url,username=cred['UserName'],password=pw,timeout=25,ssl_verify_cert=True,require_tls=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(Path.home()/'.agents/config/work-time-calendar.json'))
    parser.add_argument('--profile',default='work')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('inspect')
    p=sub.add_parser('list');p.add_argument('--from',dest='start',required=True);p.add_argument('--to',dest='end',required=True)
    p=sub.add_parser('create');p.add_argument('--event',required=True)
    args=parser.parse_args();config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'));profile=select_profile(config,args.profile)
    client=open_client(profile)
    try:
        cal=select_calendar(client.principal(),profile['calendar_name'])
        if args.command=='inspect':result={'connected':True,'calendar':profile['calendar_name'],'provider':'caldav','color_support':'unknown'}
        elif args.command=='list':
            a=date_time({'dateTime':args.start});b=date_time({'dateTime':args.end})
            if b<=a:raise ValueError('End must be later than start')
            result={'items':[normalize(x) for x in cal.search(start=a,end=b,event=True,expand=True)]}
        else:
            event=json.loads(Path(args.event).read_text(encoding='utf-8-sig'));uid=event_uid(event)
            create_calendar_event(cal,event)
            observed=normalize(cal.get_event_by_uid(uid))
            if observed['summary']!=event['summary'] or observed['description']!=event['description'] or datetime.fromisoformat(observed['start'])!=date_time(event['start']) or datetime.fromisoformat(observed['end'])!=date_time(event['end']):raise ConnectorError('Readback mismatch; do not retry without checking calendar')
            color=event_color(event)
            if color and observed.get('color','').casefold()!=color.casefold():raise ConnectorError('Color readback mismatch; check calendar before retry')
            result={'created':True,'event':observed,'color_support':'property_readback'}
        print(json.dumps(result,ensure_ascii=False))
    finally:client.close()

if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    try:main()
    except Exception as e:
        print(json.dumps({'error_type':type(e).__name__,'message':'Операция не выполнена или результат неизвестен; проверьте подключение и календарь до повторной записи.'},ensure_ascii=False),file=sys.stderr);sys.exit(1)
