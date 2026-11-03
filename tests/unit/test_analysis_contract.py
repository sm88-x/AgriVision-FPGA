"""Real CPU inference: batch structure, image identity and shared CPU/board algorithm settings."""
from pathlib import Path
import argparse,json,shutil,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src/app'))
import cv2
import numpy as np
import comparison_runtime as cpu
import scene_runtime as scene

class AnalysisContractTests(unittest.TestCase):
    def test_preprocess_only_does_not_load_model_or_invent_objects(self):
        dest=ROOT/'outputs/tests/preprocess_only';dest.mkdir(parents=True,exist_ok=True)
        args=argparse.Namespace(image=str(ROOT/'data/datasets/seeds/raw/soybean_sparse_001.png'),
             directory=None,species='soybean',gt=None,board=None,frame_id=None,preprocess_only=True,
             out=None,pheno_out=str(dest/'frames.json'))
        with patch.object(scene,'Engine',side_effect=AssertionError('model must not load')):
            scene.offline(args)
        frame=json.loads((dest/'frames.json').read_text(encoding='utf-8'))['frames'][0]
        self.assertFalse(frame['analysis_enabled']);self.assertEqual(frame['objects'],[])
        self.assertFalse(frame['scene']['hardware']);self.assertEqual(frame['fpga_file'],'')
        self.assertTrue(Path(frame['image_file']).is_file())

    def test_real_batch_frames_images_and_matching_algorithms(self):
        dest=ROOT/'outputs/tests/analysis_contract';inputs=dest/'inputs'
        inputs.mkdir(parents=True,exist_ok=True)
        for name in ['soybean_sparse_001.png','soybean_sparse_002.png','redbean_sparse_001.png']:
            shutil.copy2(ROOT/'data/datasets/seeds/raw'/name,inputs/name)
        args=argparse.Namespace(image=None,dir=str(inputs),species='soybean',gt=None,
            out=str(dest/'comparison.json'),pheno_out=str(dest/'phenotyping.json'))
        self.assertEqual(cpu.run(args),0)
        pheno=json.loads((dest/'phenotyping.json').read_text(encoding='utf-8'))
        comparison=json.loads((dest/'comparison.json').read_text(encoding='utf-8'))
        self.assertEqual(len(pheno['frames']),2);self.assertEqual(len(comparison['images']),2)
        for i,frame in enumerate(pheno['frames']):
            self.assertEqual(frame['frame_id'],i)
            self.assertNotIn('frames',frame,'batch must contain frames, not nested documents')
            self.assertEqual(len(frame['objects']),frame['seed_count'])
            self.assertEqual(frame['fpga_file'],'');self.assertFalse(frame['scene']['hardware'])
            expected=cpu.normalized_bgr(Path(frame['source_file']))
            actual=cv2.imread(frame['image_file'])
            np.testing.assert_array_equal(actual,expected)
        first=comparison['images'][0]
        engine=scene.Engine()
        frame,row=engine.process(cpu.normalized_bgr(inputs/'soybean_sparse_001.png'),0,'soybean',dest,'contract',board=None)
        self.assertEqual({k:v['count'] for k,v in first['modes'].items()},
                         {k:v['count'] for k,v in row['modes'].items()})
        self.assertEqual(len(frame['objects']),pheno['frames'][0]['seed_count'])
        args.gt=40
        with self.assertRaisesRegex(ValueError,'GT'):cpu.run(args)

if __name__=='__main__':unittest.main(verbosity=2)
