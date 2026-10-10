"""Synthetic deadline/checkpoint failure qualification; no network or Git pushes."""
import contextlib,io,json,os,sys,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import crawl as C

class Budget(unittest.TestCase):
    def test_budget_sticky_ceiling_and_resume_integrate_without_replay_loss(self):
        clock=[0.0];requests=[]
        class Reply(io.BytesIO):
            pass
        def fetch(req,timeout=None):
            body=json.loads(req.data);method=body['method'];params=body['params'];requests.append((req.full_url,method,params))
            if req.full_url.endswith('blocked.invalid'):
                return Reply(b'{"error":{"message":"forbidden"}}')
            clock[0]+=.2
            if method=='eth_blockNumber':value=hex(460)
            elif method=='eth_getBlockByNumber':value={'timestamp':hex(1787307825)}
            else:
                lo=int(params[0]['fromBlock'],16);hi=int(params[0]['toBlock'],16)
                if hi-lo+1>100:return Reply(b'{"error":{"message":"block range limited to 100"}}')
                clock[0]+=4
                value=[{'blockNumber':hex(lo),'transactionHash':'0x'+format(lo,'064x'),'logIndex':'0x0','topics':[C.TOPIC,'0x'+'0'*24+'1'*40,'0x'+'0'*24+'2'*40],'data':'0x1'}]
            return Reply(json.dumps({'result':value}).encode())
        with tempfile.TemporaryDirectory() as root:
            state=Path(root)/'state.json';state.write_text(json.dumps({'next_block':10,'rows_total':0,'win':200}))
            C._PREFERRED.clear()
            with patch.multiple(C,HERE=root,DATA=str(Path(root)/'data'),STATE=str(state),RPCS=['https://blocked.invalid','https://working.invalid']),patch.object(C.time,'monotonic',side_effect=lambda:clock[0]),patch.object(C.time,'sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)),patch.object(C.urllib.request,'urlopen',side_effect=fetch),patch.dict(os.environ,{'CRAWL_BUDGET_SECONDS':'12'}),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(C.main(),75)
                first=json.loads(state.read_text());self.assertGreater(first['next_block'],10);self.assertEqual(first['range_ceiling'],100)
                log_requests=[r for r in requests if r[1]=='eth_getLogs'];successful_floor=first['next_block'];self.assertEqual(sum(r[0].endswith('blocked.invalid') for r in log_requests),2)
                C._PREFERRED.clear();requests.clear()
                with patch.dict(os.environ,{'CRAWL_BUDGET_SECONDS':'240'}):self.assertIsNone(C.main())
            final=json.loads(state.read_text());self.assertEqual(final['next_block'],431)
            resumed=[r for r in requests if r[1]=='eth_getLogs'];self.assertEqual(int(resumed[0][2][0]['fromBlock'],16),successful_floor)
            self.assertTrue(all(int(r[2][0]['toBlock'],16)-int(r[2][0]['fromBlock'],16)+1<=100 for r in resumed))
            rows=[json.loads(line) for p in (Path(root)/'data').glob('*.jsonl') for line in p.read_text().splitlines()]
            self.assertEqual(len(rows),len({(r['tx'],r['li']) for r in rows}));self.assertEqual(final['rows_total'],len(rows))

    def setup_case(self,root):
        state=Path(root)/'state.json';state.write_text(json.dumps({'next_block':10,'rows_total':0,'win':1}))
        row={'blockNumber':'0xa','transactionHash':'0xabc','logIndex':'0x0','topics':[C.TOPIC,'0x'+'0'*24+'1'*40,'0x'+'0'*24+'2'*40],'data':'0x1'}
        def rpc(method,params):
            if method=='eth_blockNumber':return hex(42)
            if method=='eth_getBlockByNumber':return {'timestamp':hex(1787307825+64)}
            return [row]
        return state,rpc
    def run_case(self,root,**extra):
        state,rpc=self.setup_case(root)
        output=io.StringIO()
        with patch.multiple(C,HERE=root,DATA=str(Path(root)/'data'),STATE=str(state)),patch.object(C,'rpc',side_effect=rpc),patch.object(C,'_pause',side_effect=C.CrawlBudgetExceeded('budget')),contextlib.redirect_stdout(output):
            if extra.get('fail_save'):
                with patch.object(C,'save_state',side_effect=OSError('checkpoint failed')):
                    with self.assertRaises(OSError):C.main()
                result=None
            elif extra.get('fail_flush'):
                with patch.object(C.os,'fsync',side_effect=OSError('write failed')):
                    with self.assertRaises(OSError):C.main()
                result=None
            else:result=C.main()
        return state,result,output.getvalue()
    def test_partial_banks_flushed_rows_and_next_block(self):
        with tempfile.TemporaryDirectory() as root:
            state,result,out=self.run_case(root)
            self.assertEqual(result,75);saved=json.loads(state.read_text());self.assertEqual(saved['next_block'],11);self.assertEqual(saved['rows_total'],1)
            files=list((Path(root)/'data').glob('*.jsonl'));self.assertEqual(len(files),1);self.assertEqual(len(files[0].read_text().splitlines()),1)
            self.assertIn('catch-up incomplete',out);self.assertIsNone(C._DEADLINE)
    def test_checkpoint_failure_cannot_return_partial_marker(self):
        with tempfile.TemporaryDirectory() as root:
            state,result,out=self.run_case(root,fail_save=True)
            self.assertIsNone(result);self.assertNotIn('checkpoint saved',out);self.assertEqual(json.loads(state.read_text())['next_block'],10);self.assertIsNone(C._DEADLINE)
    def test_durable_write_failure_cannot_advance_or_mark_partial(self):
        with tempfile.TemporaryDirectory() as root:
            state,result,out=self.run_case(root,fail_flush=True)
            self.assertIsNone(result);self.assertNotIn('checkpoint saved',out);self.assertEqual(json.loads(state.read_text())['next_block'],10)
    def test_deadline_expires_during_bootstrap_as_failure(self):
        with tempfile.TemporaryDirectory() as root,patch.object(C,'rpc',side_effect=C.CrawlBudgetExceeded('budget')),patch.multiple(C,HERE=root,DATA=str(Path(root)/'data'),STATE=str(Path(root)/'state.json')):
            with self.assertRaises(C.CrawlBudgetExceeded):C.main()
            self.assertFalse((Path(root)/'state.json').exists());self.assertIsNone(C._DEADLINE)
    def test_rpc_deadline_limits_each_request_timeout(self):
        with patch.object(C,'_DEADLINE',12),patch.object(C.time,'monotonic',return_value=10),patch.object(C.urllib.request,'urlopen',return_value=io.BytesIO(b'{"result":[]}')) as fetch:
            self.assertEqual(C.rpc('eth_getLogs',[]),[]);self.assertEqual(fetch.call_args.kwargs['timeout'],2)
    def test_malformed_provider_json_is_not_range_or_budget(self):
        with patch.object(C,'RPCS',['https://invalid.example']),patch.object(C.urllib.request,'urlopen',side_effect=lambda *a,**kw:io.BytesIO(b'not JSON')),patch.object(C.time,'sleep'):
            with self.assertRaises(RuntimeError) as result:C.rpc('eth_getLogs',[])
            self.assertNotIsInstance(result.exception,(ValueError,C.CrawlBudgetExceeded))
    def test_provider_failure_at_budget_expiry_remains_failure(self):
        times=iter([10,13])
        with patch.object(C,'_DEADLINE',12),patch.object(C.time,'monotonic',side_effect=lambda:next(times)),patch.object(C,'RPCS',['https://invalid.example']),patch.object(C.urllib.request,'urlopen',side_effect=lambda *a,**kw:io.BytesIO(b'not JSON')):
            with self.assertRaises(RuntimeError) as result:C.rpc('eth_getLogs',[])
            self.assertNotIsInstance(result.exception,C.CrawlBudgetExceeded)
    def test_range_refusal_at_budget_expiry_is_not_partial(self):
        with tempfile.TemporaryDirectory() as root:
            state,rpc=self.setup_case(root);saved=json.loads(state.read_text());saved['win']=2;state.write_text(json.dumps(saved))
            def limited(method,params):
                if method=='eth_getLogs':raise ValueError('provider range limit')
                return rpc(method,params)
            with patch.multiple(C,HERE=root,DATA=str(Path(root)/'data'),STATE=str(state)),patch.object(C,'rpc',side_effect=limited),patch.object(C,'_pause',side_effect=C.CrawlBudgetExceeded()),contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(RuntimeError):C.main()
            self.assertEqual(json.loads(state.read_text()),saved);self.assertNotIn('checkpoint saved',output.getvalue())
    def test_range_refusal_then_expired_next_loop_is_not_partial(self):
        with tempfile.TemporaryDirectory() as root:
            state,rpc=self.setup_case(root);saved=json.loads(state.read_text());saved['win']=2;state.write_text(json.dumps(saved))
            def limited(method,params):
                if method=='eth_getLogs':raise ValueError('provider range limit')
                return rpc(method,params)
            with patch.multiple(C,HERE=root,DATA=str(Path(root)/'data'),STATE=str(state)),patch.object(C,'rpc',side_effect=limited),patch.object(C,'_pause'),patch.object(C,'_remaining',side_effect=[None,C.CrawlBudgetExceeded()]),contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(RuntimeError):C.main()
            self.assertEqual(json.loads(state.read_text()),saved);self.assertNotIn('checkpoint saved',output.getvalue())
    def test_budget_is_bounded(self):
        for value in ('0','241','nan'):
            with patch.dict(os.environ,{'CRAWL_BUDGET_SECONDS':value}):
                with self.assertRaises(ValueError):C.main()
    def test_crawl_step_preserves_exit_status_and_marker(self):
        s=Path(os.environ.get('WORKFLOW_REVIEW_PATH',str(Path(__file__).resolve().parents[1]/'.github/workflows/crawl.yml'))).read_text()
        code=s.split('      - name: Crawl to tip',1)[1].split('        run: |',1)[1].split('      - name:',1)[0]
        code='\n'.join(line[10:] for line in code.splitlines() if line.startswith('          '))
        with tempfile.TemporaryDirectory() as root:
            fake=Path(root)/'python3';fake.write_text('#!/bin/sh\nexit "$MOCK_RC"\n');fake.chmod(0o700)
            for rc in (0,1,75):
                output=Path(root)/'output';output.write_text('')
                env={**os.environ,'PATH':root+':'+os.environ['PATH'],'MOCK_RC':str(rc),'GITHUB_OUTPUT':str(output)}
                result=subprocess.run(['bash','-e','-c',code],env=env,capture_output=True)
                self.assertEqual(result.returncode,rc);self.assertEqual(output.read_text(),'partial=true\n' if rc==75 else '')
    def test_workflow_source_passes_original_pii_gate_and_rejects_bad_line(self):
        source=Path(os.environ.get('WORKFLOW_REVIEW_PATH',str(Path(__file__).resolve().parents[1]/'.github/workflows/crawl.yml'))).read_text()
        gate=Path(__file__).resolve().with_name('test_pii.py')
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'.github/workflows/crawl.yml';p.parent.mkdir(parents=True);p.write_text(source)
            result=subprocess.run([sys.executable,str(gate),'--tree',root],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout)
            p.write_text(source+'\n# private contact example@example.test\n') # pii-ok: synthetic rejection fixture
            result=subprocess.run([sys.executable,str(gate),'--tree',root],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
    def test_actual_workflow_cancel_restore_and_detector_guards(self):
        text=Path(__file__).resolve().parents[1].joinpath('.github/workflows/crawl.yml').read_text()
        guards={}
        for section in text.split('      - name: ')[1:]:
            name=section.splitlines()[0]
            guards[name]=next((line.strip()[4:] for line in section.splitlines()[1:] if line.strip().startswith('if: ')),'success()')
        def allows(name,failed=True,cancelled=False,partial='true',restore='success',detect='skipped',gate='success'):
            text=guards[name]
            for key,value in {'failure()':failed,'success()':not failed,'always()':True,'!cancelled()':not cancelled,'steps.crawl.outputs.partial':partial,'steps.checkpoint_gate.outcome':gate,'steps.restore_memory.outcome':restore,'steps.detect.outcome':detect}.items():text=text.replace(key,repr(value))
            return eval(text.replace('&&',' and ').replace('||',' or '),{'__builtins__':{}})
        for name in ('Validate incomplete catch-up checkpoint','Save incomplete catch-up checkpoint'):
            self.assertTrue(allows(name));self.assertFalse(allows(name,cancelled=True));self.assertFalse(allows(name,partial=''));self.assertFalse(allows(name,failed=False))
        self.assertFalse(allows('Save incomplete catch-up checkpoint',gate='failure'))
        save='Save detector memory (private repo)'
        self.assertFalse(allows(save,restore='failure',detect='failure'));self.assertFalse(allows(save,detect='skipped'))
        self.assertTrue(allows(save,detect='failure'));self.assertTrue(allows(save,detect='success',failed=True)) # notify failure still saves attempted detector state
        self.assertFalse(allows(save,detect='success',cancelled=True))
    def test_workflow_guard_has_no_green_or_private_save_partial_path(self):
        s=Path(os.environ.get('WORKFLOW_REVIEW_PATH', str(Path(__file__).resolve().parents[1]/'.github/workflows/crawl.yml'))).read_text()
        self.assertIn('steps.crawl.outputs.partial',s);self.assertIn("steps.checkpoint_gate.outcome == 'success'",s)
        self.assertIn("steps.restore_memory.outcome == 'success'",s);self.assertIn("(steps.detect.outcome == 'success' || steps.detect.outcome == 'failure')",s)
        self.assertIn('actions@users.noreply.github.com" # pii-ok:',s)
        self.assertIn('if: success()',s);self.assertIn('exit "$rc"',s)

if __name__=='__main__':unittest.main()
