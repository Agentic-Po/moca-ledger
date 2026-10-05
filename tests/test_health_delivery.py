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
