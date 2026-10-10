"""Bounded public-provider fallback and replay-safe crawl regression."""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import crawl as C

class CrawlRpc(unittest.TestCase):
    def setUp(self): C._PREFERRED.clear()

    def test_working_provider_sticky_per_method_and_fails_over(self):
        calls=[]
        def fetch(req, **kw):
            calls.append(req.full_url)
            if req.full_url.endswith("a.invalid"): raise RuntimeError("blocked")
            return io.BytesIO(b'{"result":[]}')
        with patch.object(C,"RPCS",["https://a.invalid","https://b.invalid"]),patch.object(C.urllib.request,"urlopen",side_effect=fetch):
            C.rpc("eth_getLogs",[]); C.rpc("eth_getLogs",[])
        self.assertEqual(calls,["https://a.invalid","https://b.invalid","https://b.invalid"])
        self.assertNotIn("eth_blockNumber",C._PREFERRED)

    def test_range_ceiling_persists_across_resume_without_repeat_growth(self):
        with tempfile.TemporaryDirectory() as root:
            state=os.path.join(root,"state.json"); widths=[]
            with open(state,"w") as f:json.dump({"next_block":10,"rows_total":0,"win":400},f)
            def rpc(method,params):
                if method=="eth_blockNumber":return hex(459)
                if method=="eth_getBlockByNumber":return {"timestamp":hex(1787307825)}
                width=int(params[0]["toBlock"],16)-int(params[0]["fromBlock"],16)+1; widths.append(width)
                if width>100:raise ValueError("provider range limit")
                return []
            with patch.multiple(C,HERE=root,DATA=os.path.join(root,"data"),STATE=state),patch.object(C,"rpc",side_effect=rpc),patch.object(C.time,"sleep"):C.main()
            saved=json.load(open(state)); self.assertEqual(saved["range_ceiling"],100);self.assertEqual(widths,[400,200,100,100,100,100,20])
            saved["next_block"]=10; json.dump(saved,open(state,"w"));widths.clear()
            with patch.multiple(C,HERE=root,DATA=os.path.join(root,"data"),STATE=state),patch.object(C,"rpc",side_effect=rpc),patch.object(C.time,"sleep"):C.main()
            self.assertEqual(widths,[100,100,100,100,20])

    def test_range_provider_does_not_hide_working_provider(self):
        replies = [io.BytesIO(b'{"error":{"message":"limited to 50 blocks"}}'),
                   io.BytesIO(b'{"result":[]}')]
        with patch.object(C, 'RPCS', ['https://a.invalid', 'https://b.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=replies), patch.object(C.time, 'sleep'):
            self.assertEqual(C.rpc('eth_getLogs', []), [])

    def test_http400_range_body_shrinks(self):
        error = urllib.error.HTTPError('https://a.invalid', 400, 'bad request', {}, io.BytesIO(b'{"error":{"code":35,"message":"ranges over 10000 blocks are not supported"}}'))
        with patch.object(C, 'RPCS', ['https://a.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=error), patch.object(C.time, 'sleep'):
            with self.assertRaises(ValueError): C.rpc('eth_getLogs', [])

    def test_rate_limit_does_not_shrink_range(self):
        with patch.object(C, 'RPCS', ['https://a.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(b'{"error":{"message":"rate limit: too many requests"}}')), patch.object(C.time, 'sleep'):
            with self.assertRaises(RuntimeError): C.rpc('eth_getLogs', [])
        self.assertFalse(C._range_error('monthly quota limit exceeded'))
        self.assertTrue(C._range_error('eth_getLogs is limited to 0 - 50 blocks range'))

    def test_null_result_is_not_successful_empty_range(self):
        with patch.object(C, 'RPCS', ['https://a.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(b'{"result":null}')), patch.object(C.time, 'sleep'):
            with self.assertRaises(RuntimeError): C.rpc('eth_getLogs', [])

    def test_outage_is_bounded_and_sanitized(self):
        with patch.object(C, 'RPCS', ['https://a.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=RuntimeError('https://private.invalid/?key=secret')), patch.object(C.time, 'sleep'), self.assertRaises(RuntimeError) as caught:
            C.rpc('eth_getLogs', [])
        self.assertNotIn('secret', str(caught.exception))

    def test_failed_smallest_range_never_advances_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            state = os.path.join(root, 'state.json')
            with open(state, 'w') as f: json.dump({'next_block':10, 'rows_total':0, 'win':2},f)
            def rpc(method, params):
                if method == 'eth_blockNumber': return hex(42)
                if method == 'eth_getBlockByNumber': return {'timestamp':hex(1787307825)}
                raise ValueError('provider range limit')
            with patch.multiple(C, HERE=root, DATA=os.path.join(root,'data'), STATE=state), patch.object(C,'rpc',side_effect=rpc), patch.object(C.time,'sleep'):
                with self.assertRaises(RuntimeError): C.main()
            self.assertEqual(json.load(open(state))['next_block'], 10)

    def test_replay_append_before_checkpoint_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            state=os.path.join(root,'state.json');data=os.path.join(root,'data');os.mkdir(data)
            stamp=1787307825
            row={'block':10,'ts':stamp,'tx':'0xabc','li':0,'from':'0x'+'1'*40,'to':'0x'+'2'*40,'value':'1'}
            day=C.dt.datetime.fromtimestamp(stamp,C.dt.UTC).strftime('%Y-%m-%d')
            path=os.path.join(data,day+'.jsonl')
            with open(path,'w') as f:f.write(json.dumps(row)+'\n')
            with open(state,'w') as f:json.dump({'next_block':10,'rows_total':0,'win':1},f)
            log={'blockNumber':hex(10),'transactionHash':'0xabc','logIndex':'0x0','topics':[C.TOPIC,'0x'+'0'*24+'1'*40,'0x'+'0'*24+'2'*40],'data':'0x1'}
            def rpc(method, params):
                if method=='eth_blockNumber':return hex(40)
                if method=='eth_getBlockByNumber':return {'timestamp':hex(stamp+60)}
                return [log]
            with patch.multiple(C,HERE=root,DATA=data,STATE=state),patch.object(C,'rpc',side_effect=rpc),patch.object(C.time,'sleep'):C.main()
            self.assertEqual(len(open(path).readlines()),1)
            saved=json.load(open(state));self.assertEqual(saved['next_block'],11);self.assertEqual(saved['rows_total'],1)

if __name__=='__main__':unittest.main()
