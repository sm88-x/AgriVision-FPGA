"""HTTP/TCP loopback fixtures only: these tests are NOT physical-board evidence."""
from pathlib import Path
import copy, hashlib, json, socket, struct, subprocess, sys, threading, time, unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/app'))
import numpy as np
import cv2
import scene_board_client as client
import network_backend as network
import timestamped_frame_protocol as protocol

@contextmanager
def fixture_server(mutate=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            data=self.rfile.read(int(self.headers['Content-Length']))
            w=int(self.headers['X-Width']);h=int(self.headers['X-Height'])
            frame=int(self.headers['X-Frame-Id'])
            pixels=bytes([97])*(w*h)
            config={'edge_mode':0,'threshold':40,'morph_mode':2,'enable_gray':1,'enable_gauss':1,'out_sel':0}
            expected={'width':w,'height':h,'mode':0,'threshold':40,'morph_mode':2,'enable_gray':1,'enable_gauss':1,'out_sel':0}
            meta={'request_id':self.headers['X-Request-Id'],'frame_id':frame,'mode':self.headers['X-Control-Mode'],
                  'width':w,'height':h,'hardware':True,'simulated':False,'register_verified':True,
                  'applied_frame':frame,'input_sha256':hashlib.sha256(data).hexdigest(),
                  'output_sha256':hashlib.sha256(pixels).hexdigest(),'fpga_dma_ms':4.0,
                  'applied_config':config,'actual_registers':[{'name':k,'value':v,'offset':hex(16+i*8)} for i,(k,v) in enumerate(expected.items())],
                  'receipt_note':'SYNTHETIC UNIT-TEST FIXTURE; NOT BOARD EVIDENCE'}
            if mutate: mutate(meta)
            encoded=json.dumps(meta).encode()
            body=struct.pack('!I',len(encoded))+encoded+pixels
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield '127.0.0.1:%d'%server.server_port
    finally:server.shutdown();server.server_close();thread.join(2)

class BoardHttpTests(unittest.TestCase):
    def test_valid_fixture_and_wrong_receipts(self):
        image=np.arange(8*8*3,dtype=np.uint8).reshape(8,8,3)
        with fixture_server() as address:
            out,receipt=client.process_board(image,7,address,'manual')
            self.assertEqual(out.shape,(8,8));self.assertTrue(np.all(out==97))
            self.assertGreaterEqual(receipt['transport_rtt_ms'],0)
        invalid={
            'simulated': lambda m:m.update(simulated=True),
            'not_hardware':lambda m:m.update(hardware=False),
            'unverified':lambda m:m.update(register_verified=False),
            'wrong_frame':lambda m:m.update(frame_id=8),
            'wrong_applied_frame':lambda m:m.update(applied_frame=8),
            'wrong_request':lambda m:m.update(request_id='stale-request'),
            'wrong_input':lambda m:m.update(input_sha256='0'*64),
            'wrong_output':lambda m:m.update(output_sha256='0'*64),
            'wrong_size':lambda m:m.update(width=9),
            'wrong_mode':lambda m:m.update(mode='auto'),
            'missing_registers':lambda m:m.update(actual_registers=None),
            'wrong_register_value':lambda m:m['actual_registers'][0].update(value=99),
            'missing_timing':lambda m:m.update(fpga_dma_ms=None),
            'invalid_timing':lambda m:m.update(fpga_dma_ms=float('nan')),
        }
        for name,mutate in invalid.items():
            with self.subTest(name=name),fixture_server(mutate) as address:
                with self.assertRaises(RuntimeError):client.process_board(image,7,address,'manual')

    def run_network(self,dest,use_board,address='127.0.0.1:1'):
        dest.mkdir(parents=True,exist_ok=True)
        server=network.make_server(0);port=server.getsockname()[1]
        class Detector:
            state='SYNTHETIC_TEST_DETECTOR'
            def detect_seeds(self,*args,**kwargs):return []
        host,bport=address.rsplit(':',1)
        request={'frames':1,'width':32,'height':24,'detect':False,'net_fpga':use_board,
                 'board_host':host,'board_http_port':int(bport),'net_port':port,'board_timeout':.3}
        errors=[]
        def run():
            try:network.run_net_source(request,Detector(),dest,server)
            except Exception as exc:errors.append(str(exc))
        thread=threading.Thread(target=run,daemon=True);thread.start()
        ok,png=cv2.imencode('.jpg',np.full((24,32,3),128,np.uint8));self.assertTrue(ok)
        with socket.create_connection(('127.0.0.1',port),timeout=2) as sock:
            sock.sendall(protocol.pack_frame(23,time.time_ns(),32,24,png.tobytes()))
        thread.join(8);self.assertFalse(thread.is_alive(),'network worker did not release listener/reader')
        status=json.loads((dest/'live_status.json').read_text(encoding='utf-8'))
        return status,errors

    def test_production_http_service_imports_and_marks_mock(self):
        dest=ROOT/'outputs/tests/http_loopback/production_service'
        dest.mkdir(parents=True,exist_ok=True)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        log=(dest/'server.log').open('wb')
        proc=subprocess.Popen([sys.executable,'-B',str(ROOT/'hardware/board/scene_config_server.py'),
            '--host','127.0.0.1','--port',str(port),'--mock','--bitfile','mock.bit',
            '--log',str(dest/'receipts.jsonl')],cwd=ROOT/'outputs/tests',stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+8
            while True:
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.2):break
                except OSError:
                    if proc.poll() is not None or time.monotonic()>deadline:
                        self.fail('production HTTP service failed to start; see '+str(dest/'server.log'))
                    time.sleep(.05)
            with self.assertRaisesRegex(RuntimeError,'模拟'):
                client.process_board(np.zeros((24,32,3),np.uint8),0,'127.0.0.1:%d'%port,'manual')
        finally:
            if proc.poll() is None:proc.terminate()
            proc.wait(timeout=5);log.close()

    def test_image_sender_uses_agf1_header(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(5)
            port=listener.getsockname()[1]
            proc=subprocess.Popen([sys.executable,'-B',str(ROOT/'src/app/timestamped_frame_sender.py'),
                '--host','127.0.0.1','--port',str(port),'--images',
                str(ROOT/'data/datasets/seeds/raw/soybean_sparse_001.png'),'--frames','1'],
                cwd=ROOT/'outputs/tests',stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                with listener.accept()[0] as conn:
                    conn.settimeout(5)
                    length=struct.unpack('>I',network.recv_exact(conn,4))[0]
                    body=network.recv_exact(conn,length)
                    head=protocol.parse_header(body)
                    self.assertEqual(head['frame_id'],0)
                    self.assertGreater(head['capture_ts_ns'],0)
                    self.assertEqual((head['width'],head['height']),(640,480))
                stdout,stderr=proc.communicate(timeout=5)
                self.assertEqual(proc.returncode,0,stderr)
            finally:
                if proc.poll() is None:proc.kill();proc.communicate()

    def test_network_local_never_calls_board(self):
        status,errors=self.run_network(ROOT/'outputs/tests/http_loopback/local',False)
        self.assertFalse(errors);self.assertEqual(status['fpga'],'NOT_USED')
        self.assertEqual(status['board_receipt'],{})
        self.assertFalse((ROOT/'outputs/tests/http_loopback/local/preview/board_latest.png').exists())

    def test_network_board_uses_returned_pixels(self):
        with fixture_server() as address:
            dest=ROOT/'outputs/tests/http_loopback/board'
            status,errors=self.run_network(dest,True,address)
        self.assertFalse(errors);self.assertEqual(status['state'],'FINISHED')
        self.assertTrue(status['board_receipt']['hardware'])
        with (dest/'network_frames.csv').open(encoding='utf-8',newline='') as f:
            import csv
            rows=list(csv.DictReader(f))
        self.assertGreater(float(rows[0]['processing_fps']),0)
        self.assertIn('fps',rows[0])  # Receiver rate and completed-frame rate are separate fields.

        out=cv2.imread(str(dest/'preview/board_latest.png'),cv2.IMREAD_GRAYSCALE)
        self.assertTrue(np.all(out==97),'preview must use returned pixels rather than local gray')

    def test_sender_disconnect_invalidates_live_receipt(self):
        dest=ROOT/'outputs/tests/http_loopback/disconnect'
        dest.mkdir(parents=True,exist_ok=True)
        stop=dest/'stop.request'
        if stop.exists():stop.unlink()
        server=network.make_server(0);port=server.getsockname()[1]
        class Detector:
            state='SYNTHETIC_TEST_DETECTOR'
        errors=[]
        with fixture_server() as address:
            host,bport=address.rsplit(':',1)
            request={'frames':0,'width':32,'height':24,'detect':False,'net_fpga':True,
                     'board_host':host,'board_http_port':int(bport),'net_port':port}
            def run():
                try:network.run_net_source(request,Detector(),dest,server)
                except Exception as exc:errors.append(str(exc))
            thread=threading.Thread(target=run,daemon=True);thread.start()
            try:
                ok,jpeg=cv2.imencode('.jpg',np.zeros((24,32,3),np.uint8));self.assertTrue(ok)
                with socket.create_connection(('127.0.0.1',port),timeout=2) as sock:
                    sock.sendall(protocol.pack_frame(1,time.time_ns(),32,24,jpeg.tobytes()))
                deadline=time.monotonic()+6
                while True:
                    try:status=json.loads((dest/'live_status.json').read_text(encoding='utf-8'))
                    except (OSError,ValueError):status={}
                    if status.get('state')=='DISCONNECTED':break
                    if time.monotonic()>deadline:self.fail('disconnect did not invalidate state')
                    time.sleep(.05)
                self.assertEqual(status['board_receipt'],{});self.assertEqual(status['preview'],{})
                self.assertEqual(status['latency'],{});self.assertEqual(status['fpga'],'OFFLINE')
            finally:
                stop.write_text('stop');thread.join(8)
            self.assertFalse(thread.is_alive());self.assertFalse(errors)

    def test_network_failure_invalidates_old_board_state(self):
        with fixture_server(lambda m:m.update(simulated=True)) as address:
            dest=ROOT/'outputs/tests/http_loopback/failure'
            dest.mkdir(parents=True,exist_ok=True)
            (dest/'live_status.json').write_text(json.dumps({'fpga':'ONLINE','preview':{'board':'old.png'}}))
            status,errors=self.run_network(dest,True,address)
        self.assertTrue(errors);self.assertEqual(status['state'],'ERROR')
        self.assertFalse(status['running']);self.assertEqual(status['preview'],{})
        self.assertEqual(status['board_receipt'],{});self.assertEqual(status['latency'],{})

if __name__=='__main__':unittest.main(verbosity=2)
