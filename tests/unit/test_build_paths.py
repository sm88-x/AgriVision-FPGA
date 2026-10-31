"""CLI build paths must remain project-relative even from another launch directory."""
import importlib.util,sys,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('agrivision_build',ROOT/'tools/build.py')
build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
class BuildPaths(unittest.TestCase):
    def test_relative_out_is_under_project_root(self):
        with patch.object(sys,'argv',['build.py','vivado','--out','outputs/path_test','--stage','validate']),patch.object(build,'run') as run:
            build.main()
        command,cwd=run.call_args.args
        self.assertEqual(command[-2],ROOT/'outputs/path_test')
        self.assertEqual(command[-1],'validate');self.assertEqual(cwd,ROOT)
    def test_absolute_out_is_preserved(self):
        target=ROOT/'outputs/absolute_path_test'
        with patch.object(sys,'argv',['build.py','vivado','--out',str(target)]),patch.object(build,'run') as run:
            build.main()
        self.assertEqual(run.call_args.args[0][-2],target)
if __name__=='__main__':unittest.main(verbosity=2)
