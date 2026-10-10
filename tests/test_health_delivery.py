"""Synthetic transport and source-health regressions; never use live credentials."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notify import health, weekly, watchdog, telegram, request_enrichment, state_sync

class HealthDelivery(unittest.TestCase):
    def test_failure_notice_copy_preserves_severity_state_and_receipts(self):
        from copy import deepcopy
        baseline={'open':{'synthetic':{'status':'acked'}},'last_failure_post':1,'last_run_ok':False}
        states=[]
        for published in (False,True):
            with patch('notify.coverage_clock.cutoff_lines',return_value=['Saved scan cutoff: synthetic']),patch.object(telegram,'load_state',return_value=deepcopy(baseline)),patch.object(telegram.time,'time',return_value=30000),patch.object(telegram,'save_state') as save,patch.object(telegram,'send',return_value={'ok':True,'result':{'message_id':123}}) as send,patch.object(telegram,'_log_out') as receipt:
                self.assertEqual(telegram.failure('https://example.invalid/run',checkpoint_published=published),0)
                states.append(save.call_args.args[0]);self.assertFalse(send.call_args.kwargs['silent'])
                self.assertEqual(receipt.call_args.args[1],'notice');self.assertEqual(send.call_count,1)
                text=send.call_args.args[0];self.assertTrue(text.startswith('🔴'))
                if published:
                    self.assertIn('checkpoint saved',text);self.assertIn('detection did not run',text)
                    self.assertNotIn('advanced',text);self.assertNotIn('healthy',text)
                else:self.assertEqual(text,'🔴 <b>detector run failed</b>\nhttps://example.invalid/run')
        self.assertEqual(states[0],states[1]);self.assertFalse(states[0]['last_run_ok'])
        self.assertEqual(states[0]['open'],baseline['open'])

    def test_failure_notice_dedup_policy_unchanged_for_both_copies(self):
        for published in (False,True):
            for last_ok,expected in ((True,0),(False,1)):
                with patch('notify.coverage_clock.cutoff_lines',return_value=['Saved scan cutoff: synthetic']),patch.object(telegram,'load_state',return_value={'last_failure_post':100,'last_run_ok':last_ok}),patch.object(telegram.time,'time',return_value=101),patch.object(telegram,'save_state'),patch.object(telegram,'send',return_value={'ok':False}) as send,patch.object(telegram,'_log_out'):
                    self.assertEqual(telegram.failure('https://example.invalid/run',checkpoint_published=published),0)
                    self.assertEqual(send.call_count,expected)

    def test_failure_before_restore_writes_only_local_state_and_receipt(self):
        from notify import msglog
        import io,contextlib
        for published in (False,True):
            with tempfile.TemporaryDirectory() as td:
                root=Path(td);local=root/'msglog'
                with patch('notify.coverage_clock.cutoff_lines',return_value=['Saved scan cutoff: synthetic']),patch.object(telegram,'STATE',root/'state.json'),patch.object(telegram,'send',return_value={'ok':True,'result':{'message_id':123}}),patch.object(msglog,'LOCAL',local),patch.object(msglog,'INDEX',local/'index.json'),patch.object(msglog,'_INDEX',None),patch.object(msglog,'_DIRTY',False),patch.dict(sys.modules,{'msglog':msglog}),patch.object(state_sync,'push') as detector_push,patch.object(msglog,'push') as receipt_push,patch.object(telegram.urllib.request,'urlopen',side_effect=AssertionError('unexpected network')) as network,contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(telegram.failure('https://example.invalid/run',checkpoint_published=published),0)
                    msglog.flush()
                    self.assertTrue((root/'state.json').exists());self.assertTrue((local/'index.json').exists())
                    detector_push.assert_not_called();receipt_push.assert_not_called();network.assert_not_called()
                    self.assertNotIn('Coverage is still behind',output.getvalue())

    def test_retained_membership_warning(self):
        text = health.mindset_warning(dict(mindset_source='hashed-stale', mindset_generated_at='2026-10-01T12:00:00Z', mindset_age_h=70))
        self.assertIn('2026-10-01T12:00:00Z', text)
        self.assertIn('remain in use alongside chain discovery', text)
        self.assertNotIn('chain alone', text)
        self.assertIn('chain discovery only', health.mindset_warning(dict(mindset_source='chain-only')))
        self.assertIsNone(health.mindset_warning(dict(mindset_source='hashed')))

    def weekly_run(self, response):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root/'alerts').mkdir()
            with patch.object(weekly, 'ROOT', root), patch.object(health, 'load', return_value={}), patch.object(weekly.urllib.request, 'urlopen', side_effect=OSError), patch.object(telegram, 'send', return_value=response), patch.object(telegram, '_log_out') as receipt:
                rc = weekly.main()
                return rc, (root/'alerts/weekly.json').exists(), receipt.call_count

    def test_heartbeat_reports_source_and_pending_checks_without_crossing_count(self):
        state = {'open': {'active': {'needs_triage':True}, 'closed': {'needs_triage':True,'status':'closed'}, 'archived': {'needs_triage':True,'status':'archived'}, 'held': {'status':'reported'}}}
        hb = dict(ledger_last='2026-10-06 01:32',mindset_generated_at='2026-10-05T15:10:29Z',mindset_source='hashed',detect_ok=True,fires_last_24h_total=987654)
        with patch.object(telegram,'load_state',return_value=state), patch.object(telegram,'_hb_doc',return_value=hb), patch.object(telegram,'send',return_value={'ok':True,'result':{'message_id':123}}) as send, patch.object(telegram,'save_state'), patch.object(telegram,'_log_out'):
            self.assertEqual(telegram.heartbeat(force=True),0)
            body=send.call_args.args[0]
            self.assertIn('Detection running',body)
            self.assertIn('06 Oct 2026 09:32 SGT',body)
            self.assertIn('05 Oct 2026 23:10 SGT',body)
            self.assertIn('Private verification pending: 1 check.',body)
            self.assertIn('1 case(s)',body)
            self.assertNotIn('987654',body)
            self.assertIn('not an all-clear',body)
            self.assertEqual(telegram._sgt_time('<unexpected>'),'not available')

    def test_weekly_requires_delivery_receipt(self):
        for response in ({'ok':False}, {'ok':True,'result':{}}):
            self.assertEqual(self.weekly_run(response), (3,False,0))
        self.assertEqual(self.weekly_run({'ok':True,'result':{'message_id':123}}), (0,True,1))

    def test_dispatch_clock_survives_concurrent_state_save(self):
        self.assertEqual(state_sync.merge({"private_dispatch_at": 200}, {"private_dispatch_at": 100})["private_dispatch_at"], 200)

    def test_private_dispatch_due_and_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'state.json'
            state = dict(triage_version=1, private_dispatch_at=10000, open={'synthetic':dict(id='synthetic',needs_triage=True,triage_requested=True,triage_ts=10000,tier='notify')})
            with patch.object(request_enrichment,'STATE',path), patch.object(request_enrichment.time,'time',return_value=12000) as clock, patch.dict('os.environ',{'PRIVATE_REPO_PAT':'synthetic'}), patch.object(request_enrichment,'dispatch',return_value=(True,'accepted')) as dispatch, patch.object(state_sync,'push',return_value=True):
                path.write_text(json.dumps(state))
                self.assertEqual(request_enrichment.main(),0); dispatch.assert_not_called()
                clock.return_value=13601
                self.assertEqual(request_enrichment.main(),0); self.assertEqual(dispatch.call_count,1)
                self.assertEqual(json.loads(path.read_text())['private_dispatch_at'],13601)
                self.assertEqual(request_enrichment.main(),0); self.assertEqual(dispatch.call_count,1)
                state=json.loads(path.read_text()); state['open']={}; path.write_text(json.dumps(state))
                clock.return_value=13601+6*3600
                self.assertEqual(request_enrichment.main(),0); self.assertEqual(dispatch.call_count,2)
                state=json.loads(path.read_text()); state['open']={'urgent':dict(id='urgent',needs_triage=True,triage_requested=False,signal='S-X')}; path.write_text(json.dumps(state))
                self.assertEqual(request_enrichment.main(),0); self.assertEqual(dispatch.call_count,3)

    def test_watchdog_dedup_retry_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)/'state.json'
            hb = dict(mindset_source='hashed-stale', mindset_generated_at='2026-10-01T00:00:00Z', mindset_age_h=70)
            with patch.object(watchdog,'STATE',state), patch.object(health,'load',return_value=hb), patch.object(watchdog.time,'time',return_value=1000000) as clock, patch.object(watchdog,'send',return_value={'ok':True,'result':{'message_id':123}}) as send, patch.object(telegram,'_log_out'):
                self.assertEqual(watchdog.main(),0); self.assertEqual(send.call_count,1)
                clock.return_value += 7*3600
                self.assertEqual(watchdog.main(),0); self.assertEqual(send.call_count,1)
                hb['mindset_generated_at']='2026-10-02T00:00:00Z'
                self.assertEqual(watchdog.main(),0); self.assertEqual(send.call_count,2)
                clock.return_value += 25*3600
                send.return_value={'ok':True,'result':{}}
                self.assertEqual(watchdog.main(),3)
                send.return_value={'ok':True,'result':{'message_id':124}}
                self.assertEqual(watchdog.main(),0); self.assertEqual(send.call_count,4)
                hb['mindset_source']='hashed'
                self.assertEqual(watchdog.main(),0)
                self.assertNotIn('mindset_stale',json.loads(state.read_text())['watchdog'])
                hb['mindset_source']='hashed-stale'
                self.assertEqual(watchdog.main(),0); self.assertEqual(send.call_count,5)

if __name__ == '__main__': unittest.main()
