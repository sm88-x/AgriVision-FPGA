"""Launch the current GUI with a project-relative backend and this Python."""
from pathlib import Path
import argparse,subprocess,sys,os,shutil
ROOT=Path(__file__).resolve().parents[1]
ap=argparse.ArgumentParser(description=__doc__)
ap.add_argument('--qt-bin',type=Path,help='Qt runtime bin if not in PATH')
ap.add_argument('args',nargs=argparse.REMAINDER)
a=ap.parse_args();env=os.environ.copy()
if a.qt_bin:env['PATH']=str(a.qt_bin.resolve())+os.pathsep+env['PATH']
exe=next((p for p in [ROOT/'outputs/qt/release/agrivision_gui.exe',ROOT/'outputs/qt/agrivision_gui.exe'] if p.exists()),None)
if exe is None:ap.error('Build GUI first: python tools/build.py qt')
args=a.args[1:] if a.args[:1]==['--'] else a.args
raise SystemExit(subprocess.call([str(exe),'--backend',str(ROOT/'src/software/app/network_backend.py'),'--python',sys.executable,*args],cwd=ROOT,env=env))
