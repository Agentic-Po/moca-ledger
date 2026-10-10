#!/usr/bin/env python3
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from notify.coverage_clock import cutoff_lines
from notify import telegram

NOW = dt.datetime(2026, 10, 10, 5, 54, tzinfo=dt.UTC)
STAMP = dt.datetime(2026, 10, 10, 1, 0, tzinfo=dt.UTC)

def header(number):
    return dict(number=hex(number), hash='0x'+'a'*64, timestamp=hex(int(STAMP.timestamp())))

class ClockTests(unittest.TestCase):
    def run_clock(self, state, read=header):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); file=root/'state.json'; file.write_text(json.dumps(state))
            before=file.read_bytes(); body=cutoff_lines(root, NOW, read, start_block=1)
            self.assertEqual(file.read_bytes(),before)
            return '\n'.join(body)
    def test_exact_cutoff_and_lag(self):
        body=self.run_clock(dict(next_block=101))
        self.assertIn('10 Oct 2026 09:00 SGT · 4h 54m behind',body)
        self.assertIn('Saved block: 100',body)
        self.assertIn('As of: 10 Oct 2026 13:54 SGT',body)
    def test_unknown_or_invalid_cursor_never_requests_header(self):
        for state in ({},dict(next_block=True),dict(next_block=0),dict(next_block='100')):
            body=self.run_clock(state,lambda n:self.fail('invalid cursor requested'))
            self.assertIn('no valid saved cursor',body)
    def test_initial_cursor_has_no_verified_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'state.json').write_text('{"next_block":101}')
            body='\n'.join(cutoff_lines(tmp,NOW,lambda n:self.fail('initial cursor requested'),start_block=101))
            self.assertIn('no valid saved cursor',body)
    def test_bad_header_is_explicitly_unavailable(self):
        for change in (dict(number='0x99'),dict(hash='secret'),dict(timestamp='bad'),dict(timestamp=hex(int(NOW.timestamp())+1))):
            body=self.run_clock(dict(next_block=101),lambda n:dict(header(n),**change))
            self.assertIn('block 100 · time unavailable',body)
            self.assertNotIn('secret',body)
    def test_header_failure_does_not_fabricate_time(self):
        def fail(n):raise OSError('private diagnostic')
        body=self.run_clock(dict(next_block=101),fail)
        self.assertIn('time unavailable',body); self.assertNotIn('private diagnostic',body)
    def test_sgt_date_rollover(self):
        early=dt.datetime(2026,10,9,18,tzinfo=dt.UTC)
        body=self.run_clock(dict(next_block=101),lambda n:dict(header(n),timestamp=hex(int(early.timestamp()))))
        self.assertIn('10 Oct 2026 02:00 SGT',body)
    def test_existing_rpc_budget_restored(self):
        import crawl
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'state.json').write_text('{"next_block":101}')
            with patch.object(crawl,'_DEADLINE',123),patch.object(crawl,'rpc',return_value=header(100)) as rpc:
                cutoff_lines(tmp,NOW,start_block=1)
                rpc.assert_called_once_with('eth_getBlockByNumber',['0x64',False])
                self.assertEqual(crawl._DEADLINE,123)
    def test_rpc_exception_restores_prior_deadline_and_hides_diagnostic(self):
        import crawl
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp,'state.json').write_text('{"next_block":101}')
            with patch.object(crawl,'_DEADLINE',123),patch.object(crawl,'rpc',side_effect=OSError('private diagnostic')):
                body='\n'.join(cutoff_lines(tmp,NOW,start_block=1))
                self.assertEqual(crawl._DEADLINE,123)
                self.assertIn('time unavailable',body)
                self.assertNotIn('private diagnostic',body)
    def test_only_verified_publication_gets_cutoff_notice(self):
        for published in (False,True):
            state={}
            with patch.object(telegram,'load_state',return_value=state),patch.object(telegram,'save_state'),patch.object(telegram,'send',return_value={'ok':True}) as send,patch.object(telegram,'_log_out'),patch('notify.coverage_clock.cutoff_lines',return_value=['synthetic cutoff']) as clock:
                telegram.failure('https://example.invalid/run',published)
                body=send.call_args.args[0]
                if published:
                    clock.assert_called_once(); self.assertIn('synthetic cutoff',body); self.assertIn('🟡 <b>Data is catching up · checkpoint saved</b>',body); self.assertNotIn('🔴',body)
                else:
                    clock.assert_not_called(); self.assertIn('detector run failed',body)
                self.assertNotIn('@',body)

if __name__=='__main__':unittest.main()
