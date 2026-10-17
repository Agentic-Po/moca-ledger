#!/usr/bin/env python3
"""Private, receipt-tracked workflow failure/recovery episodes.

Separate from detector memory: a crawl/restore/publication failure must survive a
fresh runner without ever overwriting findings. Public logs remain number-free.
"""
import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(1,str(ROOT))
from notify import state_sync,telegram
from notify.privlog import private_print
API=f'https://api.github.com/repos/{state_sync.REPO}/contents/state/run-health.json'
RUN=re.compile(r'https://github\.com/Agentic-Po/moca-ledger/actions/runs/[0-9]+\Z')
LABELS={'chain':'Chain collection','state_restore':'Detector memory restore','state_persistence':'Detector memory persistence','oracle_restore':'Detection source restore','triage_restore':'Verification decisions restore','detection':'Detection','privacy_gate':'Publication privacy checks','behaviour_gate':'Alert behaviour checks','reply_poll':'Telegram reply polling','alerts':'Outgoing alerts','watchdog':'Source-health checks','daily_heartbeat':'Daily Telegram heartbeat','message_log':'Telegram message ledger','publication':'On-chain data publication','enrichment_dispatch':'Private enrichment dispatch','uptime_ping':'External uptime confirmation','recovery_tracking':'Recovery delivery or tracking','workflow':'Workflow completion'}

class StoreUnavailable(Exception):pass

def valid(state):
 if not isinstance(state,dict) or state.get('schema')!=1:raise StoreUnavailable()
 active=state.get('active')
 if active is not None:
  if not isinstance(active,dict) or not re.fullmatch('[a-f0-9]{16}',active.get('id','')) or not RUN.fullmatch(active.get('run_url','')) or not isinstance(active.get('components'),list) or not active['components'] or any(k not in LABELS for k in active['components']):raise StoreUnavailable()
 return state

def load():
 try:
  value=state_sync._req('GET',api=API)
  if not value or not value.get('content'):raise StoreUnavailable()
  raw=base64.b64decode(value['content'],validate=False)
  if len(raw)>64000:raise StoreUnavailable()
  return valid(json.loads(raw)),value.get('sha')
 except urllib.error.HTTPError as exc:
  if exc.code==404:return {'schema':1,'active':None},None
  raise StoreUnavailable() from None
 except Exception:raise StoreUnavailable() from None

def save(state,sha):
 raw=json.dumps(valid(state),sort_keys=True,separators=(',',':')).encode()
 if len(raw)>64000:raise StoreUnavailable()
 body={'message':'Update private workflow health','content':base64.b64encode(raw).decode()}
 if sha:body['sha']=sha
 # A lost PUT response is reconciled by reading back the exact intended document.
 for attempt in range(2):
  try:
   result=state_sync._req('PUT',body,api=API)
   if not result or not result.get('content',{}).get('sha'):raise StoreUnavailable()
   return result['content']['sha']
  except Exception:
   try:
    remote,remote_sha=load()
    if remote==state:return remote_sha
    if attempt==0 and remote_sha==sha:continue
   except StoreUnavailable:pass
   raise StoreUnavailable() from None
 raise StoreUnavailable()

def _now():return dt.datetime.now(dt.UTC).timestamp()
def _iso(value):return dt.datetime.fromtimestamp(value,dt.UTC).isoformat()
def _url(value):
 if not RUN.fullmatch(value or ''):raise ValueError('invalid run reference')
 return value

def _send(text,silent=False):
 # Telegram has no idempotency key. Never blindly retry an ambiguous send.
 return telegram._post('sendMessage',{'chat_id':telegram.CHAT(),'text':text,'parse_mode':'HTML','disable_web_page_preview':'true','disable_notification':'true' if silent else 'false'},attempts=1)

def _receipt(result):
 mid=(result.get('result') or {}).get('message_id') if isinstance(result,dict) else None
 return type(mid) is int and mid>0 and result.get('ok') is True

def _known_failure(result):
 return isinstance(result,dict) and result.get('ok') is False and not result.get('ambiguous')

def _coverage():
 from notify.coverage_clock import cutoff_lines
 return [html.escape(line) for line in cutoff_lines(ROOT)]

def _failure_text(active,checkpoint):
 if checkpoint:
  start='🟡 <b>Data is catching up · checkpoint saved</b>'
  detail='Checks have not run on the unscanned range yet. Scheduled runs will continue catch-up.'
 elif active['components']==['reply_poll']:
  start='🟡 <b>Telegram replies temporarily unavailable</b>'
  detail='The run reached notification processing, but could not read Telegram replies. Outgoing delivery is checked separately.'
 else:
  start='🔴 <b>Detection workflow needs attention</b>'
  detail='Failed stage: '+', '.join(LABELS[k] for k in active['components'])+'.'
 return '\n'.join([start,detail,*_coverage(),active['run_url']])

def _recovery_text(active,url,now):
 warnings=telegram._blind_reasons(telegram._hb_doc())
 start='🟡 <b>Workflow recovered · source coverage still limited</b>' if warnings else '🟢 <b>Workflow recovered</b>'
 parts=[start,'Restored: '+', '.join(LABELS[k] for k in active['components'])+'.']
 if active.get('known_recovered_at'):parts.append('First successful follow-up: '+telegram._sgt_time(active['known_recovered_at']))
 parts+=['Confirmed: '+telegram._sgt_time(_iso(now)),*_coverage()]
 if warnings:parts+=['Source note: '+html.escape(str(w)) for w in warnings]
 parts+=['One recovery confirmation for the earlier warning; routine successful runs stay quiet.',url]
 return '\n'.join(parts)

def _deliver(state,sha,kind,text,now):
 active=state['active'];key=kind+'_delivery';previous=active.get(key,{})
 if previous.get('phase') in ('sending','uncertain'):
  private_print('Run health: delivery is unconfirmed; refusing a blind resend',public='run-health: delivery unconfirmed')
  return 4,sha
 active[key]={'phase':'sending','attempted_at':now}
 sha=save(state,sha)  # Durable intent before any external message.
 result=_send(text,silent=kind=='recovery')
 if _receipt(result):
  active[key]={'phase':'sent','at':now,'message_id':result['result']['message_id']}
  try:sha=save(state,sha)
  except StoreUnavailable:
   private_print('Run health: receipt persistence failed; intent remains for reconciliation',public='run-health: persistence failed');return 4,sha
  telegram._log_out(result,'notice')
  return 0,sha
 active[key]={'phase':'pending' if _known_failure(result) else 'uncertain','attempted_at':now}
 sha=save(state,sha)
 private_print('Run health: message delivery not confirmed',public='run-health: delivery unconfirmed')
 return 3,sha

def failure(url,components=None,checkpoint_published=False,now=None):
 url=_url(url);now=_now() if now is None else now
 components=sorted(set(components or ['workflow']))
 if any(k not in LABELS for k in components):raise ValueError('invalid failure component')
 try:
  state,sha=load();active=state.get('active')
  # A delivery/receipt problem at the final recovery step already has durable intent.
  # Do not turn it into a second incident or duplicate notice.
  if active and components==['recovery_tracking']:return 0
  previous_components=active.get('components') if active else None
  if not active:
   active={'id':hashlib.sha256((url+':'+str(now)).encode()).hexdigest()[:16],'run_url':url,'started_at':now,'components':components}
   state['active']=active
  active.update(run_url=url,last_failed_at=now,components=components,checkpoint_published=bool(checkpoint_published))
  # A fresh failed attempt revokes any previously prepared recovery for this episode.
  active.pop('recovery_delivery',None);active.pop('known_recovered_at',None)
  sha=save(state,sha)
  delivery=active.get('failure_delivery',{})
  if previous_components==components and delivery.get('phase')=='sent' and now-delivery['at']<6*3600:return 0
  if delivery.get('phase') in ('sending','uncertain'):return 4
 except StoreUnavailable:
  private_print('Run health: private episode storage unavailable',public='run-health: storage unavailable')
  # Preserve a best-effort warning even when the failure was before detector restore.
  try:_send(_failure_text({'run_url':url,'components':components},checkpoint_published))
  except Exception:pass
  return 3
 try:
  rc,_=_deliver(state,sha,'failure',_failure_text(active,checkpoint_published),now)
  return rc
 except StoreUnavailable:
  # A send may already have happened; never fall back to a second message.
  private_print('Run health: failure delivery could not be persisted',public='run-health: persistence failed')
  return 3

def success(url,now=None):
 url=_url(url);now=_now() if now is None else now
 try:
  state,sha=load();active=state.get('active')
  if not active:return 0
  prior=active.get('recovery_delivery',{})
  if prior.get('phase')!='sent':
   rc,sha=_deliver(state,sha,'recovery',_recovery_text(active,url,now),now)
   if rc:return rc
  # The delivered receipt is persisted before closing, so a failed close retries
  # only the close and never re-sends the recovery message.
  state['last_recovery']={'episode_id':active['id'],'at':now,'run_url':url,'message_id':active['recovery_delivery']['message_id'],'failed_run_url':active['run_url'],'components':active['components']}
  state['active']=None;save(state,sha)
  return 0
 except StoreUnavailable:
  private_print('Run health: recovery could not be verified or persisted',public='run-health: storage unavailable');return 3

def _components():
 try:
  values=json.loads(os.environ.get('NOTIFY_FAILURES','{}'))
  found=[k for k,v in values.items() if k in LABELS and v=='failed']
 except (ValueError,TypeError,AttributeError):found=[]
 stage=os.environ.get('FAILURE_STAGE','workflow')
 if stage in LABELS and stage!='workflow':found.append(stage)
 return sorted(set(found)) or ['workflow']

def main():
 parser=argparse.ArgumentParser();choice=parser.add_mutually_exclusive_group(required=True)
 choice.add_argument('--failure');choice.add_argument('--success');choice.add_argument('--note',choices=sorted(LABELS));parser.add_argument('--checkpoint-published',action='store_true')
 args=parser.parse_args()
 if args.note:
  with open(os.environ['GITHUB_OUTPUT'],'a') as out:out.write(args.note+'=failed\n')
  return 0
 rc=failure(args.failure,_components(),args.checkpoint_published) if args.failure else success(args.success)
 private_print('Run health command completed',public='run-health: ok' if rc==0 else 'run-health: failed')
 return rc

if __name__=='__main__':raise SystemExit(main())
