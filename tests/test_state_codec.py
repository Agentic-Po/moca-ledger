import base64
import gzip
import json
import unittest
from unittest.mock import patch
from notify import state_codec as c

class StateCodecTests(unittest.TestCase):
    def state(self):
        return {'open':{'synthetic':{'status':'watching','ack_note':'retain unicode ✓','verification_txs':['0x'+'a'*64,'0x'+'b'*64]}},'by_message':{'123':'synthetic'},'retired':7,'pending_send':False}
    def test_legacy_bytes_and_lossless_deterministic_roundtrip(self):
        state=self.state();raw=json.dumps(state,indent=1).encode()
        self.assertEqual(c.decode_bytes(raw),raw)
        self.assertEqual(c.decode_state(c.encode_state(state)),state)
        self.assertEqual(c.encode_state(state),c.encode_state(state))
    def test_damage_or_unknown_format_fails_closed(self):
        packet=json.loads(c.encode_state(self.state()))
        for change in ({'sha256':'0'*64},{'decoded_bytes':1},{'decoded_bytes':True},{'data':'invalid!'},{'state_encoding':'unknown'},{'extra':True}):
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):
                c.decode_state(json.dumps({**packet,**change}).encode())
        for payload in (b'[]',b'null',b'{"open":[]}',b'{'):
            with self.assertRaises(ValueError):c.decode_state(payload)
    def test_bounded_decode_rejects_bomb_and_concatenated_streams(self):
        packet=json.loads(c.encode_state(self.state()))
        packet['data']=base64.b64encode(gzip.compress(b'x'*100000)).decode();packet['decoded_bytes']=10
        with self.assertRaises(ValueError):c.decode_state(json.dumps(packet).encode())
        packet=json.loads(c.encode_state(self.state()));packet['data']=base64.b64encode(base64.b64decode(packet['data'])+gzip.compress(b'extra')).decode()
        with self.assertRaises(ValueError):c.decode_state(json.dumps(packet).encode())
        with patch.object(c,'MAX_DECODED_BYTES',10):
            with self.assertRaises(ValueError):c.encode_state(self.state())
    def test_nested_envelope_is_not_a_logical_state(self):
        with self.assertRaises(ValueError):c.encode_state({'state_encoding':c.ENCODING})

class StateSyncReaderTests(unittest.TestCase):
    def test_pull_and_conflict_reader_decode_and_preserve_on_failure(self):
        import tempfile
        from pathlib import Path
        from notify import state_sync as s
        state={'open':{'case':{'status':'watching','verification_txs':['evidence']}}}
        with tempfile.TemporaryDirectory() as td, patch.object(s,'STATE',Path(td)/'state.json'), patch.object(s,'_pat',return_value='synthetic'):
            for raw in (json.dumps(state).encode(), c.encode_state(state)):
                response={'content':base64.b64encode(raw).decode(),'sha':'synthetic'}
                with patch.object(s,'_req',return_value=response):
                    self.assertTrue(s.pull())
                    self.assertEqual(json.loads(s.STATE.read_bytes()),state)
                    self.assertEqual(s._remote_state(),(state,'synthetic'))
            before=s.STATE.read_bytes()
            with patch.object(s,'_req',return_value={'content':base64.b64encode(b'{"state_encoding":"unknown"}').decode(),'sha':'bad'}):
                self.assertFalse(s.pull())
                self.assertIsNone(s._remote_state())
                self.assertEqual(s.STATE.read_bytes(),before)

class StateSyncWriterTests(unittest.TestCase):
    def test_push_preserves_evidence_and_merges_concurrent_legacy_or_compressed(self):
        import tempfile
        import urllib.error
        from pathlib import Path
        from notify import state_sync as s
        local={'open':{'local':{'id':'local','verification_txs':['proof']}}}
        remote={'open':{'remote':{'id':'remote','status':'watching','ack_note':'retain'}}}
        for remote_raw in (json.dumps(remote).encode(),c.encode_state(remote)):
            with tempfile.TemporaryDirectory() as td, patch.object(s,'STATE',Path(td)/'state.json'), patch.object(s,'_pat',return_value='synthetic'), patch.object(s.time,'sleep'):
                s.STATE.write_text(json.dumps(local))
                calls=[]
                def request(method,body=None):
                    if method=='GET':return {'content':base64.b64encode(remote_raw).decode(),'sha':'current'}
                    calls.append(body)
                    if len(calls)==1:raise urllib.error.HTTPError('synthetic',409,'conflict',{},None)
                    return {'content':{'sha':'saved'}}
                with patch.object(s,'_req',side_effect=request):self.assertTrue(s.push())
                saved=c.decode_state(base64.b64decode(calls[-1]['content']))
                self.assertEqual(saved['open'],{**remote['open'],**local['open']})
                self.assertEqual(json.loads(s.STATE.read_bytes()),saved)
                self.assertEqual(calls[-1]['sha'],'current')
                self.assertIn('state_encoding',json.loads(base64.b64decode(calls[-1]['content'])))
    def test_unreadable_concurrent_state_is_never_overwritten(self):
        import tempfile
        import urllib.error
        from pathlib import Path
        from notify import state_sync as s
        with tempfile.TemporaryDirectory() as td, patch.object(s,'STATE',Path(td)/'state.json'), patch.object(s,'_pat',return_value='synthetic'):
            s.STATE.write_text('{"open":{}}')
            with patch.object(s,'_req',side_effect=urllib.error.HTTPError('synthetic',409,'conflict',{},None)) as req, patch.object(s,'_remote_state',return_value=None):
                self.assertFalse(s.push())
                self.assertEqual(req.call_count,1)
