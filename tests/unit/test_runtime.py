"""No board/camera: real loopback protocol, bounded failures and inference provenance."""
from pathlib import Path
import sys,os,json,time,socket,subprocess,hashlib,unittest
ROOT=Path(__file__).resolve().parents[2]
for rel in ['src/software/app','src/software/ai/inference','hardware/board']:
    sys.path.insert(0,str(ROOT/rel))
OUT=ROOT/'outputs/tests';OUT.mkdir(parents=True,exist_ok=True)
class RuntimeTests(unittest.TestCase):
    def test_offline_analysis_rejects_board_without_outputs(self):
        dest=OUT/'unsupported_board';dest.mkdir(exist_ok=True)
        output=dest/'comparison.json'
        if output.exists():output.unlink()
        cp=subprocess.run([sys.executable,'-B',str(ROOT/'src/software/app/comparison_runtime.py'),
            '--image','missing.png','--species','soybean','--board','127.0.0.1:9000',
            '--out',str(output)],capture_output=True,timeout=30)
        self.assertNotEqual(cp.returncode,0)
        self.assertIn(b'--board',cp.stderr)
        self.assertFalse(output.exists())

    def test_board_refusal_is_terminal_and_invalidates_preview(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        dest=OUT/'refused';dest.mkdir(exist_ok=True)
        req={'mode':'image','source':'board','detect':False,'detector':'mock',
             'image_path':str(ROOT/'data/datasets/seeds/raw/soybean_sparse_001.png'),
             'width':320,'height':240,'host':'127.0.0.1','port':port,
             'timeout':0.2,'output_dir':str(dest)}
        path=dest/'request.json';path.write_text(json.dumps(req),encoding='utf-8')
        start=time.monotonic()
        cp=subprocess.run([sys.executable,'-B',str(ROOT/'src/software/app/network_backend.py'),'--request',str(path)],capture_output=True,timeout=15)
        self.assertNotEqual(cp.returncode,0,cp.stdout)
        status=json.loads((dest/'live_status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['state'],'ERROR');self.assertFalse(status['running'])
        self.assertEqual(status['preview'],{});self.assertLess(time.monotonic()-start,10)
    def test_mock_server_real_tcp_protocol(self):
        from pc_client import FrameClient
        import frame_protocol as fp
        import numpy as np
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        dest=OUT/'loopback';dest.mkdir(exist_ok=True)
        log=(dest/'server.log').open('wb')
        proc=subprocess.Popen([sys.executable,'-B',str(ROOT/'hardware/board/frame_server.py'),
           '--mock','--host','127.0.0.1','--port',str(port),'--frames','1','--out',str(dest),'--quiet'],stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+8
            while True:
                try:client=FrameClient('127.0.0.1',port,1).connect();break
                except OSError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.05)
            # Complete protocol request and Golden comparison via the production client.
            import pc_client
            gray=np.arange(256*256,dtype=np.uint8).reshape(256,256)
            params={'mode':0,'threshold':40,'morph_mode':0,'enable_gauss':0,
                    'pipeline_mode':fp.PIPELINE_EXPLICIT}
            from live_backend import send_one
            try:row,board,response=send_one(client,gray,params,'explicit',frame_id=0,do_golden=True)
            finally:client.close()
            self.assertEqual(row['status'],'OK');self.assertEqual(row['different_pixels'],0)
            proc.wait(timeout=8);self.assertEqual(proc.returncode,0)
        finally:
            if proc.poll() is None:proc.terminate();proc.wait(timeout=5)
            log.close()
    def test_model_repeated_input_same_result(self):
        from infer_seed import SeedDetector
        import cv2
        det=SeedDetector(device='cpu')
        paths=sorted((ROOT/'data/evidence/hardware/image_run01_pc').glob('raw_bgr_*.png'))[-5:]
        self.assertEqual(len(paths),5)
        rows=[]
        for p in paths:
            image=cv2.imread(str(p));self.assertIsNotNone(image)
            a=det.detect_seeds(image,p.name);b=det.detect_seeds(image,p.name)
            self.assertEqual(a,b)
            rows.append({'image':p.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'count':len(a),'repeat_equal':True,'detections':a})
        report={'inference':det.inference_manifest(),'runs':rows,'total':sum(r['count'] for r in rows),'note':'Current CPU environment; not a claim that historical 73/78 root cause is proved'}
        (OUT/'model_repeatability.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
if __name__=='__main__':unittest.main(verbosity=2)
