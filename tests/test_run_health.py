"""Synthetic workflow-health episodes; never use Telegram credentials or live state."""
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from notify import run_health as h,telegram
URL='https://github.com/Agentic-Po/moca-ledger/actions/runs/123'
NEXT='https://github.com/Agentic-Po/moca-ledger/actions/runs/124'
OK={'ok':True,'result':{'message_id':123}}

class EpisodeTests(unittest.TestCase):
 def setUp(self):
  self.state={'schema':1,'active':None};self.revision=0;self.sent=[];self.writes=[]
  def load():return deepcopy(self.state),str(self.revision)
  def save(value,sha):
   self.assertEqual(sha,str(self.revision));self.state=deepcopy(value);self.writes.append(deepcopy(value));self.revision+=1;return str(self.revision)
  self.loader=load;self.saver=save
  for obj,name,value in [(h,'load',load),(h,'save',save),(h,'_send',lambda text,silent=False:self.sent.append((text,silent)) or deepcopy(OK)),(h,'_coverage',lambda:['Saved scan cutoff: synthetic']),(telegram,'_hb_doc',lambda:{}),(telegram,'_blind_reasons',lambda hb:[]),(telegram,'_log_out',lambda *a:None)]:
   p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)
 def fail(self):return h.failure(URL,['reply_poll'],now=100)
 def test_healthy_runs_are_quiet(self):
  self.assertEqual(h.success(NEXT,now=200),0);self.assertEqual(self.sent,[]);self.assertEqual(self.writes,[])
 def test_failure_intent_and_receipt_are_private_and_durable(self):
  self.assertEqual(self.fail(),0);active=self.state['active'];self.assertEqual(active['failure_delivery']['phase'],'sent');self.assertEqual(active['failure_delivery']['message_id'],123)
  self.assertNotIn('open',self.state);self.assertTrue(self.sent[0][0].startswith('🟡'));self.assertFalse(self.sent[0][1])
  self.assertEqual(self.writes[1]['active']['failure_delivery']['phase'],'sending')
 def test_repeated_failure_dedup_and_new_stage_warning(self):
  self.fail();self.assertEqual(h.failure(URL,['reply_poll'],now=200),0);self.assertEqual(len(self.sent),1)
  self.assertEqual(h.failure(NEXT,['publication'],now=201),0);self.assertEqual(len(self.sent),2)
 def test_one_recovery_after_full_success_then_quiet(self):
  self.fail();self.assertEqual(h.success(NEXT,now=200),0);self.assertIsNone(self.state['active']);self.assertEqual(len(self.sent),2);self.assertIn('Workflow recovered',self.sent[-1][0]);self.assertIn('Telegram reply polling',self.sent[-1][0]);self.assertTrue(self.sent[-1][1]);self.assertEqual(self.state['last_recovery']['message_id'],123)
  self.assertEqual(h.success(NEXT,now=201),0);self.assertEqual(len(self.sent),2)
 def test_unrelated_findings_and_detector_state_are_never_pushed(self):
  with patch('notify.state_sync.push') as push,patch.object(telegram,'save_state') as findings:
   self.fail();h.success(NEXT,now=200);push.assert_not_called();findings.assert_not_called()
 def test_definitively_failed_recovery_retries_on_next_success(self):
  self.fail()
  with patch.object(h,'_send',return_value={'ok':False,'error':'http429'}):self.assertEqual(h.success(NEXT,now=200),3)
  self.assertIsNotNone(self.state['active']);self.assertEqual(self.state['active']['recovery_delivery']['phase'],'pending');self.assertEqual(h.success(NEXT,now=300),0);self.assertIsNone(self.state['active'])
 def test_ambiguous_recovery_does_not_blindly_resend_or_close(self):
  self.fail()
  with patch.object(h,'_send',return_value={'ok':False,'ambiguous':True}) as send:
   self.assertEqual(h.success(NEXT,now=200),3);self.assertEqual(h.success(NEXT,now=300),4);self.assertEqual(send.call_count,1)
  self.assertEqual(self.state['active']['recovery_delivery']['phase'],'uncertain')
 def test_failed_receipt_save_does_not_resend(self):
  self.fail()
  def save(value,sha):
   if value.get('active',{}).get('recovery_delivery',{}).get('phase')=='sent':raise h.StoreUnavailable()
   return self.saver(value,sha)
  with patch.object(h,'save',side_effect=save):self.assertEqual(h.success(NEXT,now=200),4)
  count=len(self.sent);self.assertEqual(h.success(NEXT,now=300),4);self.assertEqual(len(self.sent),count)
 def test_failed_close_retries_only_close_after_saved_receipt(self):
  self.fail()
  def save(value,sha):
   if value.get('active') is None:raise h.StoreUnavailable()
   return self.saver(value,sha)
  with patch.object(h,'save',side_effect=save):self.assertEqual(h.success(NEXT,now=200),3)
  count=len(self.sent);self.assertEqual(self.state['active']['recovery_delivery']['phase'],'sent');self.assertEqual(h.success(NEXT,now=300),0);self.assertEqual(len(self.sent),count)
 def test_tracking_failure_does_not_create_duplicate_episode(self):
  self.fail();episode=self.state['active']['id'];self.assertEqual(h.failure(NEXT,['recovery_tracking'],now=200),0);self.assertEqual(len(self.sent),1);self.assertEqual(self.state['active']['id'],episode)
 def test_second_failure_after_recovery_is_new_episode(self):
  self.fail();first=self.state['active']['id'];h.success(NEXT,now=200);h.failure(NEXT,['reply_poll'],now=300);self.assertNotEqual(self.state['active']['id'],first)
 def test_sources_can_remain_degraded_after_workflow_recovers(self):
  self.fail()
  with patch.object(telegram,'_blind_reasons',return_value=['Synthetic source warning']):h.success(NEXT,now=200)
  self.assertTrue(self.sent[-1][0].startswith('🟡'));self.assertIn('Synthetic source warning',self.sent[-1][0])
 def test_retroactive_confirmation_preserves_first_success_time(self):
  self.fail();self.state['active']['known_recovered_at']='2026-10-17T01:38:00Z';h.success(NEXT,now=200);self.assertIn('17 Oct 2026 09:38 SGT',self.sent[-1][0])
 def test_no_recovery_without_readable_private_state(self):
  with patch.object(h,'load',side_effect=h.StoreUnavailable()):self.assertEqual(h.success(NEXT,now=200),3)
  self.assertEqual(self.sent,[])
 def test_intent_save_failure_prevents_recovery_send(self):
  self.fail();count=len(self.sent)
  with patch.object(h,'save',side_effect=h.StoreUnavailable()):self.assertEqual(h.success(NEXT,now=200),3)
  self.assertEqual(len(self.sent),count)
 def test_failure_before_detector_restore_still_warns_when_health_store_unavailable(self):
  with patch.object(h,'load',side_effect=h.StoreUnavailable()):self.assertEqual(h.failure(URL,['state_restore'],now=100),3)
  self.assertEqual(len(self.sent),1);self.assertIn('Detector memory restore',self.sent[0][0])
 def test_failure_ambiguous_send_and_storage_failure_never_resends(self):
  def save(value,sha):
   if (value.get('active') or {}).get('failure_delivery',{}).get('phase')=='uncertain':raise h.StoreUnavailable()
   return self.saver(value,sha)
  with patch.object(h,'save',side_effect=save),patch.object(h,'_send',return_value={'ok':False,'ambiguous':True}) as send:
   self.assertEqual(self.fail(),3);self.assertEqual(send.call_count,1)
  self.assertEqual(self.state['active']['failure_delivery']['phase'],'sending')
 def test_empty_or_invalid_receipt_never_closes_recovery(self):
  for result in [{'ok':True,'result':{}},{'ok':True,'result':{'message_id':True}}]:
   self.state={'schema':1,'active':None};self.fail()
   with patch.object(h,'_send',return_value=result):self.assertEqual(h.success(NEXT,now=200),3)
   self.assertIsNotNone(self.state['active'])
 def test_public_logs_never_reveal_run_ids_or_components(self):
  self.fail();out=io.StringIO()
  with patch.dict(os.environ,{'GITHUB_ACTIONS':'true'}),patch.object(h,'load',side_effect=h.StoreUnavailable()),contextlib.redirect_stdout(out):h.success(NEXT,now=200)
  self.assertEqual(out.getvalue(),'run-health: storage unavailable\n')
 def test_component_context_is_validated(self):
  with patch.dict(os.environ,{'NOTIFY_FAILURES':'{"reply_poll":"failed","secret":"failed"}','FAILURE_STAGE':'publication'}):self.assertEqual(h._components(),['publication','reply_poll'])
  with patch.dict(os.environ,{'NOTIFY_FAILURES':'invalid','FAILURE_STAGE':'untrusted'}):self.assertEqual(h._components(),['workflow'])
 def test_real_run_reference_only(self):
  with self.assertRaises(ValueError):h.success('https://example.invalid',now=200)

class TransportAndPersistence(unittest.TestCase):
 def test_operational_sender_has_no_blind_transport_retry(self):
  with patch.object(telegram.urllib.request,'urlopen',side_effect=OSError('synthetic')),patch.object(telegram.time,'sleep') as sleep,patch.object(telegram,'TOK',return_value='placeholder'):
   result=telegram._post('sendMessage',{},attempts=1)
  self.assertTrue(result['ambiguous']);sleep.assert_not_called()
 def test_server_error_is_ambiguous_and_not_retried(self):
  import urllib.error
  exc=urllib.error.HTTPError('https://example.invalid',502,'unavailable',{},None)
  with patch.object(telegram.urllib.request,'urlopen',side_effect=exc),patch.object(telegram,'TOK',return_value='placeholder'),patch.object(telegram.time,'sleep') as sleep:
   result=telegram._post('sendMessage',{},attempts=1)
  self.assertTrue(result['ambiguous']);sleep.assert_not_called()
 def test_lost_put_response_is_reconciled_without_resending(self):
  value={'schema':1,'active':None}
  with patch.object(h.state_sync,'_req',side_effect=TimeoutError()),patch.object(h,'load',return_value=(value,'new-sha')):self.assertEqual(h.save(value,'old-sha'),'new-sha')
 def test_foreign_state_is_not_overwritten(self):
  value={'schema':1,'active':None}
  with patch.object(h.state_sync,'_req',side_effect=TimeoutError()),patch.object(h,'load',return_value=({'schema':1,'active':None,'other':'changed'},'other-sha')):
   with self.assertRaises(h.StoreUnavailable):h.save(value,'old-sha')
 def test_workflow_recovery_follows_every_success_prerequisite(self):
  workflow=(h.ROOT/'.github/workflows/crawl.yml').read_text();i=workflow.index('- name: Confirm recovery when needed')
  for name in ['- name: Notify (page/notify/digest)','- name: Save detector memory (private repo)','- name: Commit state','- name: Request tier-2 enrichment (private side)','- name: Heartbeat ping (success)']:self.assertLess(workflow.index(name),i)
  self.assertIn("if: success() && steps.detect.outcome == 'success'",workflow[i:]);self.assertIn('python3 notify/run_health.py "${args[@]}" || true',workflow);self.assertIn('PRIVATE_REPO_PAT:',workflow[i:])

if __name__=='__main__':unittest.main()
