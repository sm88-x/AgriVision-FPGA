"""Run offline checks without a board or camera; output stays under outputs/tests."""
from pathlib import Path
import sys,subprocess,ast,json,re,time
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'outputs/tests';OUT.mkdir(parents=True,exist_ok=True)
checks=[]
def run(name,args,timeout=120):
    started=time.time()
    cp=subprocess.run([sys.executable,'-B',*args],cwd=ROOT,capture_output=True,timeout=timeout)
    (OUT/(name+'.log')).write_bytes(cp.stdout+b'\n'+cp.stderr)
    checks.append({'name':name,'passed':cp.returncode==0,'returncode':cp.returncode,'seconds':round(time.time()-started,2)})
    print(name, 'PASS' if cp.returncode==0 else 'FAIL',flush=True)
def main():
    errors=[];count=0
    for base in ['src','hardware','tests','tools','skill']:
        for p in (ROOT/base).rglob('*.py'):
            if '__pycache__' in p.parts:continue
            try:ast.parse(p.read_text(encoding='utf-8-sig'));count+=1
            except Exception as e:errors.append({'path':p.relative_to(ROOT).as_posix(),'error':str(e)})
    checks.append({'name':'python_syntax','passed':not errors,'files':count,'errors':errors})
    links=[]
    for p in ROOT.rglob('*.md'):
        if any(x in {'.git','outputs','submission'} for x in p.parts):continue
        for _,target in re.findall(r'\[([^\]]*)\]\(([^)]+)\)',p.read_text(encoding='utf-8-sig')):
            if '://' not in target and not target.startswith('#') and not (p.parent/target.split('#')[0]).exists():links.append([p.relative_to(ROOT).as_posix(),target])
    checks.append({'name':'markdown_links','passed':not links,'broken':links})
    run('historical_evidence',['tests/evidence/verify_evidence.py','--project','.'])
    run('golden_invariants',['tests/unit/pipeline_golden.py'])
    run('morphology',['tests/simulation/verify_sobel_v4_morph.py'])
    run('frame_service_mock',['hardware/board/frame_service.py','--selftest','2','--mock'])
    run('dataset_manifest',['src/software/algorithm/seed_separation.py','check'])
    run('runtime_regressions',['tests/unit/test_runtime.py'])
    run('build_paths',['tests/unit/test_build_paths.py'])
    result={'date':'2026-10-31','scope':'OFFLINE ONLY; no board or camera','checks':checks,'passed':all(c['passed'] for c in checks)}
    (OUT/'validation_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (ROOT/'tests/evidence/validation_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
