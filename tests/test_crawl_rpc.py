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
    def setUp(self):
        C._PREFERRED.clear(); C._DENIED.clear(); C._COOLDOWN.clear()

    def test_working_provider_sticky_per_method_and_fails_over(self):
        calls=[]
        def fetch(req, **kw):
            calls.append(req.full_url)
            if req.full_url.endswith("a.invalid"): raise RuntimeError("blocked")
            return io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":[]}')
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
        replies = [io.BytesIO(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"limited to 50 blocks"}}'),
                   io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":[]}')]
        with patch.object(C, 'RPCS', ['https://a.invalid', 'https://b.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=replies), patch.object(C.time, 'sleep'):
            self.assertEqual(C.rpc('eth_getLogs', []), [])

    def test_http400_range_body_shrinks(self):
        error = urllib.error.HTTPError('https://a.invalid', 400, 'bad request', {}, io.BytesIO(b'{"jsonrpc":"2.0","id":1,"error":{"code":35,"message":"ranges over 10000 blocks are not supported"}}'))
        with patch.object(C, 'RPCS', ['https://a.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=error), patch.object(C.time, 'sleep'):
            with self.assertRaises(ValueError): C.rpc('eth_getLogs', [])

    def test_rate_limit_does_not_shrink_range(self):
        with patch.object(C, 'RPCS', ['https://a.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"rate limit: too many requests"}}')), patch.object(C.time, 'sleep'):
            with self.assertRaises(RuntimeError): C.rpc('eth_getLogs', [])
        self.assertFalse(C._range_error('monthly quota limit exceeded'))
        self.assertTrue(C._range_error('eth_getLogs is limited to 0 - 50 blocks range'))

    def test_null_result_is_not_successful_empty_range(self):
        with patch.object(C, 'RPCS', ['https://a.invalid']), patch.object(C.urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(b'{"jsonrpc":"2.0","id":1,"result":null}')), patch.object(C.time, 'sleep'):
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
            row={'block':10,'ts':stamp,'tx':'0x'+'a'*64,'li':0,'from':'0x'+'1'*40,'to':'0x'+'2'*40,'value':'1'}
            day=C.dt.datetime.fromtimestamp(stamp,C.dt.UTC).strftime('%Y-%m-%d')
            path=os.path.join(data,day+'.jsonl')
            with open(path,'w') as f:f.write(json.dumps(row)+'\n')
            with open(state,'w') as f:json.dump({'next_block':10,'rows_total':0,'win':1},f)
            log={'blockNumber':hex(10),'transactionHash':'0x'+'a'*64,'logIndex':'0x0','topics':[C.TOPIC,'0x'+'0'*24+'1'*40,'0x'+'0'*24+'2'*40],'data':'0x'+'0'*63+'1','address':C.TOK,'removed':False,'blockHash':'0x'+'b'*64}
            def rpc(method, params):
                if method=='eth_blockNumber':return hex(40)
                if method=='eth_getBlockByNumber':return {'timestamp':hex(stamp+60)}
                return [log]
            with patch.multiple(C,HERE=root,DATA=data,STATE=state),patch.object(C,'rpc',side_effect=rpc),patch.object(C.time,'sleep'):C.main()
            self.assertEqual(len(open(path).readlines()),1)
            saved=json.load(open(state));self.assertEqual(saved['next_block'],11);self.assertEqual(saved['rows_total'],1)

class TypedAndBudgetRpc(unittest.TestCase):
    def setUp(self):
        C._PREFERRED.clear(); C._DENIED.clear(); C._COOLDOWN.clear()

    def reply(self, result, **overrides):
        return io.BytesIO(json.dumps({'jsonrpc':'2.0','id':1,'result':result,**overrides}).encode())

    def test_rejects_wrong_envelope_and_typed_results(self):
        invalid = [[], None, {'result':[]}, {'jsonrpc':'2.0','id':True,'result':[]},
                   {'jsonrpc':'2.0','id':2,'result':[]},
                   {'jsonrpc':'2.0','id':1,'result':[],'error':{'code':-1,'message':'x'}}]
        for body in invalid:
            with self.subTest(body=body),patch.object(C,'RPCS',['https://a.invalid']),patch.object(C.urllib.request,'urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(body).encode())),patch.object(C.time,'sleep'):
                with self.assertRaises(RuntimeError):C.rpc('eth_getLogs',[])
        for method,value,params in [('eth_blockNumber',True,[]),('eth_blockNumber','12',[]),('eth_blockNumber','0x01',[]),('eth_getBlockByNumber',None,['0x1',False]),('eth_getBlockByNumber',{'number':'0x2','timestamp':'0x1','hash':'0x'+'a'*64},['0x1',False])]:
            with self.subTest(method=method,value=value),patch.object(C,'RPCS',['https://a.invalid']),patch.object(C.urllib.request,'urlopen',side_effect=lambda *a,**k:self.reply(value)),patch.object(C.time,'sleep'):
                with self.assertRaises(RuntimeError):C.rpc(method,params)

    def test_403_skip_is_method_scoped_and_does_not_imply_range_error(self):
        calls=[]
        def fetch(req,**kw):
            calls.append(json.loads(req.data)['method'])
            raise urllib.error.HTTPError(req.full_url,403,'forbidden',{},io.BytesIO(b''))
        with patch.object(C,'RPCS',['https://a.invalid']),patch.object(C.urllib.request,'urlopen',side_effect=fetch),patch.object(C.time,'sleep'):
            for _ in range(2):
                with self.assertRaises(RuntimeError):C.rpc('eth_getLogs',[])
            with self.assertRaises(RuntimeError):C.rpc('eth_blockNumber',[])
        self.assertEqual(calls,['eth_getLogs','eth_blockNumber'])

    def test_429_is_temporary_and_does_not_imply_range_error(self):
        clock=[0];calls=[]
        def fetch(req,**kw):
            calls.append(1)
            if len(calls)==1:raise urllib.error.HTTPError(req.full_url,429,'rate limited',{},io.BytesIO(b''))
            return self.reply([])
        with patch.object(C,'RPCS',['https://a.invalid']),patch.object(C.time,'monotonic',side_effect=lambda:clock[0]),patch.object(C.urllib.request,'urlopen',side_effect=fetch),patch.object(C.time,'sleep'):
            with self.assertRaises(RuntimeError):C.rpc('eth_getLogs',[])
            self.assertEqual(len(calls),1)
            clock[0]=31
            self.assertEqual(C.rpc('eth_getLogs',[]),[])
            self.assertEqual(len(calls),2)

    def test_real_range_evidence_survives_blocked_providers(self):
        def fetch(req,**kw):
            if req.full_url.endswith('denied.invalid'):
                raise urllib.error.HTTPError(req.full_url,403,'forbidden',{},io.BytesIO(b''))
            return io.BytesIO(b'{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"block range limited to 50"}}')
        with patch.object(C,'RPCS',['https://denied.invalid','https://range.invalid']),patch.object(C.urllib.request,'urlopen',side_effect=fetch):
            for _ in range(2):
                with self.assertRaises(ValueError):C.rpc('eth_getLogs',[])

    def test_invalid_transfer_result_fails_over_before_preference(self):
        good={'address':C.TOK,'removed':False,'blockHash':'0x'+'b'*64,'blockNumber':'0xa',
              'transactionHash':'0x'+'a'*64,'logIndex':'0x0','data':'0x'+'0'*63+'1',
              'topics':[C.TOPIC,'0x'+'0'*24+'1'*40,'0x'+'0'*24+'2'*40]}
        params=[{'fromBlock':'0xa','toBlock':'0xa'}]
        for field,value in [('removed',0),('address','0x'+'0'*40),('blockNumber','0xb'),('topics',[]),('data','0x1')]:
            with self.subTest(field=field):
                C._PREFERRED.clear()
                replies=[self.reply([{**good,field:value}]),self.reply([good])]
                with patch.object(C,'RPCS',['https://bad.invalid','https://good.invalid']),patch.object(C.urllib.request,'urlopen',side_effect=replies):
                    self.assertEqual(C.rpc('eth_getLogs',params),[good])
                self.assertEqual(C._PREFERRED['eth_getLogs'],'https://good.invalid')

    def test_log_cycle_limits_failed_sockets_and_returns_failure(self):
        clock=[0.0];timeouts=[]
        def fetch(req,timeout):
            timeouts.append(timeout);clock[0]+=timeout
            raise TimeoutError('synthetic timeout')
        with patch.object(C,'RPCS',['https://'+str(i)+'.invalid' for i in range(5)]),patch.object(C.time,'monotonic',side_effect=lambda:clock[0]),patch.object(C.time,'sleep',side_effect=lambda n:clock.__setitem__(0,clock[0]+n)),patch.object(C.urllib.request,'urlopen',side_effect=fetch):
            with self.assertRaises(RuntimeError) as caught:C.rpc('eth_getLogs',[])
        self.assertNotIsInstance(caught.exception,C.CrawlBudgetExceeded)
        self.assertEqual(clock[0],45);self.assertTrue(all(0<t<=8 for t in timeouts))

    def test_response_after_cycle_deadline_is_not_accepted(self):
        clock=[0.0]
        def fetch(req,**kw):
            clock[0]=46;return self.reply([])
        with patch.object(C,'RPCS',['https://a.invalid']),patch.object(C.time,'monotonic',side_effect=lambda:clock[0]),patch.object(C.urllib.request,'urlopen',side_effect=fetch):
            with self.assertRaises(RuntimeError):C.rpc('eth_getLogs',[])
        self.assertNotIn('eth_getLogs',C._PREFERRED)

if __name__=='__main__':unittest.main()
