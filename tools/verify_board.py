"""Verify physical HTTP board output against the software Golden. No mock fallback."""
from pathlib import Path
import argparse,hashlib,json,sys,time

ROOT=Path(__file__).resolve().parents[1]
for rel in ['src/app','hardware/board','hardware/drivers']:sys.path.insert(0,str(ROOT/rel))
import cv2
import numpy as np
from scene_board_client import process_board
from frame_service import FrameService

def run(args):
    source=Path(args.image)
    if not source.is_absolute():source=ROOT/source
    image=cv2.imread(str(source),cv2.IMREAD_COLOR)
    if image is None:raise ValueError('Cannot read input image')
    out=Path(args.out)
    if not out.is_absolute():out=ROOT/out
    out.mkdir(parents=True,exist_ok=True)
    b,g,r=[image[:,:,i].astype(np.uint32) for i in range(3)]
    payload=((r<<16)|(g<<8)|b).astype('<u4').tobytes()
    rows=[]
    reference=FrameService(mock=True,verbose=False)
    try:
        for frame_id in range(args.frames):
            pixels,receipt=process_board(image,frame_id,f'{args.host}:{args.port}',args.mode)
            config=receipt['applied_config']
            expected=reference.process(payload,image.shape[1],image.shape[0],4,
                mode=config['edge_mode'],threshold=config['threshold'],morph_mode=config['morph_mode'],
                enable_gauss=bool(config['enable_gauss']),pipeline_mode=0,frame_id=frame_id)
            golden=np.frombuffer(expected['output'],dtype=np.uint8).reshape(pixels.shape)
            delta=np.abs(golden.astype(np.int16)-pixels.astype(np.int16))
            different=int(np.count_nonzero(delta));maximum=int(delta.max())
            row={'frame_id':frame_id,'different_pixels':different,'max_abs_diff':maximum,
                 'passed':different==0,'receipt':receipt}
            rows.append(row)
            cv2.imwrite(str(out/f'fpga_{frame_id:06d}.png'),pixels)
            print(f'frame {frame_id}: different={different} max_diff={maximum}',flush=True)
    finally:reference.close()
    result={'timestamp':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'scope':'PHYSICAL HTTP BOARD; STATIC IMAGE INPUT; NO CAMERA',
            'address':f'{args.host}:{args.port}','image':source.relative_to(ROOT).as_posix() if source.is_relative_to(ROOT) else source.name,
            'input_file_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'client_sha256':hashlib.sha256((ROOT/'src/app/scene_board_client.py').read_bytes()).hexdigest(),
            'golden_service_sha256':hashlib.sha256((ROOT/'hardware/board/frame_service.py').read_bytes()).hexdigest(),
            'frames':rows,'passed':len(rows)==args.frames and all(r['passed'] for r in rows)}
    (out/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',default='192.168.2.99');p.add_argument('--port',type=int,default=9001)
    p.add_argument('--mode',choices=['manual','auto'],default='manual')
    p.add_argument('--frames',type=int,default=10)
    p.add_argument('--image',default='data/datasets/seeds/raw/soybean_sparse_001.png')
    p.add_argument('--out',default='outputs/board_validation')
    args=p.parse_args()
    if not 1<=args.frames<=10000:p.error('--frames must be in 1..10000')
    raise SystemExit(run(args))
