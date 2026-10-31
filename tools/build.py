"""Build retained hardware variants or the recommended Qt application."""
from pathlib import Path
import argparse,os,shutil,subprocess
ROOT=Path(__file__).resolve().parents[1]
VARIANTS={'baseline':'baseline','pipelined':'pipelined','parameterized':'parameterized',
          'edge_morphology':'legacy/edge_morphology','configurable_pipeline':'legacy/configurable_pipeline',
          'integrated_pipeline':'integrated_pipeline'}
def run(args,cwd):
    print('RUN:',subprocess.list2cmdline([str(x) for x in args]),flush=True)
    subprocess.run([str(x) for x in args],cwd=cwd,check=True)
def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('target',choices=['qt','csim','hls','vivado','all'])
    ap.add_argument('--variant',choices=[*VARIANTS,'all'],default='integrated_pipeline')
    ap.add_argument('--qmake',default='qmake');ap.add_argument('--make',default='mingw32-make')
    ap.add_argument('--vitis-run',default='vitis-run');ap.add_argument('--vpp',default='v++');ap.add_argument('--vivado',default='vivado')
    ap.add_argument('--stage',choices=['validate','bit'],default='bit');ap.add_argument('--out',type=Path)
    a=ap.parse_args();output=ROOT/'outputs';output.mkdir(exist_ok=True)
    if a.variant=='all' and a.target not in {'csim','hls'}:ap.error('--variant all is supported for csim/hls')
    variants=list(VARIANTS) if a.variant=='all' else [a.variant]
    if a.target=='qt':
        qmake=Path(shutil.which(a.qmake) or a.qmake).resolve();make=Path(shutil.which(a.make) or a.make).resolve()
        os.environ['PATH']=str(make.parent)+os.pathsep+str(qmake.parent)+os.pathsep+os.environ['PATH']
        dest=output/'qt';dest.mkdir(exist_ok=True)
        run([qmake,ROOT/'src/software/ui/agri_morph_gui.pro'],dest);run([make,'-j4'],dest)
        return
    for name in variants:
        src=ROOT/'hardware/hls'/VARIANTS[name];hls_out=output/('hls_'+name)
        if a.target in {'csim','all'}:run([a.vitis_run,'--mode','hls','--csim','--config','hls_config.cfg','--work_dir',hls_out],src)
        if a.target in {'hls','all'}:run([a.vpp,'--compile','--mode','hls','--config','hls_config.cfg','--work_dir',hls_out],src)
        if a.target in {'vivado','all'}:
            if name not in {'integrated_pipeline','parameterized'}:ap.error('Vivado scripts are available for integrated_pipeline/parameterized; other variants support HLS reproduction')
            dest=a.out or output/('vivado_'+name)
            if not dest.is_absolute():dest=ROOT/dest
            dest=dest.resolve()
            script=ROOT/'hardware/build/vivado'/('integrated_pipeline' if name=='integrated_pipeline' else 'sobel_v3_param')/'create_project.tcl'
            run([a.vivado,'-mode','batch','-source',script,'-tclargs',hls_out/'hls/impl/ip',dest,a.stage],ROOT)
if __name__=='__main__':main()
