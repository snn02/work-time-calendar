"""Select, create, or remove local calendar profiles without storing credentials."""
import argparse,copy,json,os,re,sys,tempfile
from pathlib import Path
from urllib.parse import urlsplit

SECRET_FIELDS={'password','secret','token','access_token','refresh_token'}

def validate_profile(profile):
    if not isinstance(profile,dict) or profile.get('provider') not in ('google','caldav'):raise ValueError('Calendar profile not found or unsupported')
    if any(str(k).casefold() in SECRET_FIELDS for k in profile):raise ValueError('Secrets must not be stored in calendar configuration')
    allowed=({'provider','calendar_id'} if profile['provider']=='google' else {'provider','url','calendar_name','credential_target','connector','python'})
    if set(profile)-allowed:raise ValueError('Unsupported fields for calendar provider')
    if profile['provider']=='google':
        value=profile.get('calendar_id','')
        if not value or any(x in value for x in ('://','?','#','\n','\r')):raise ValueError('Google calendar ID is required; URLs and tokens are not accepted')
    if profile['provider']=='caldav':
        if not profile.get('calendar_name') or not profile.get('credential_target'):raise ValueError('Calendar name and credential reference are required')
        parts=urlsplit(profile.get('url',''))
        if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:raise ValueError('CalDAV URL must be HTTPS without embedded credentials or query')
    return profile

def resolve_profile(config,name=None):
    name=name or config.get('default');profile=config.get('connections',{}).get(name)
    if not isinstance(profile,dict) or profile.get('provider') not in ('google','caldav'):raise ValueError('Calendar profile not found or unsupported')
    if any(str(k).casefold() in SECRET_FIELDS for k in profile):raise ValueError('Secrets must not be stored in calendar configuration')
    return dict(profile,name=name,timezone=config.get('timezone','Europe/Moscow'))

def add_profile(config,name,profile,make_default=False):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}',name):raise ValueError('Invalid profile name')
    result=copy.deepcopy(config);connections=result.setdefault('connections',{})
    if name in connections:raise ValueError('Profile already exists')
    connections[name]=copy.deepcopy(validate_profile(profile))
    if make_default or not result.get('default'):result['default']=name
    result.setdefault('timezone','Europe/Moscow');return result

def remove_profile(config,name,new_default=None):
    result=copy.deepcopy(config);connections=result.get('connections',{})
    if name not in connections:raise ValueError('Profile does not exist')
    remaining=set(connections)-{name}
    if new_default and new_default not in remaining:raise ValueError('Replacement default profile not found')
    if result.get('default')==name:
        if remaining and not new_default:raise ValueError('Choose --new-default before removing the default profile')
        result['default']=new_default
    elif new_default:result['default']=new_default
    del connections[name];return result

def save_config(path,config):
    path.parent.mkdir(parents=True,exist_ok=True)
    name=None
    try:
        with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as f:
            name=f.name;json.dump(config,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(name,path);name=None
    finally:
        if name and os.path.exists(name):os.unlink(name)

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',default=str(Path.home()/'.agents/config/work-time-calendar.json'));p.add_argument('--profile')
    action=p.add_mutually_exclusive_group();action.add_argument('--create',metavar='NAME');action.add_argument('--delete',metavar='NAME');action.add_argument('--list',action='store_true')
    p.add_argument('--provider',choices=['google','caldav']);p.add_argument('--calendar-id');p.add_argument('--url');p.add_argument('--calendar-name');p.add_argument('--credential-target');p.add_argument('--connector');p.add_argument('--python');p.add_argument('--make-default',action='store_true');p.add_argument('--new-default')
    a=p.parse_args();path=Path(a.config).expanduser();cfg=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'timezone':'Europe/Moscow','default':None,'connections':{}}
    if a.create:
        if not a.provider:raise ValueError('--provider is required')
        profile={'provider':a.provider}
        for attr,key in [('calendar_id','calendar_id'),('url','url'),('calendar_name','calendar_name'),('credential_target','credential_target'),('connector','connector'),('python','python')]:
            value=getattr(a,attr)
            if value is not None:profile[key]=value
        if a.provider=='caldav':
            root=Path.home()/'.agents/tools/work-time-calendar-caldav';profile.setdefault('connector',str(root/'connector.py'));profile.setdefault('python',str(root/'venv/Scripts/python.exe'))
        updated=add_profile(cfg,a.create,profile,a.make_default);save_config(path,updated);result={'created':a.create,'default':updated['default']}
    elif a.delete:
        updated=remove_profile(cfg,a.delete,a.new_default);save_config(path,updated);result={'deleted':a.delete,'default':updated['default'],'credentials_removed':False,'events_removed':False}
    elif a.list:result={'default':cfg.get('default'),'profiles':[{'name':n,'provider':v.get('provider')} for n,v in cfg.get('connections',{}).items()]}
    else:result=resolve_profile(cfg,a.profile)
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    try:main()
    except (OSError,ValueError,TypeError,KeyError) as exc:print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);sys.exit(2)
