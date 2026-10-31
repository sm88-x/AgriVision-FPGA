"""Create a verified RAR submission excluding Git, caches, and local build outputs."""
from pathlib import Path
import argparse,subprocess,csv,hashlib,json,tempfile,os
ROOT=Path(__file__).resolve().parents[1]
def files():
    return sorted(p for p in ROOT.rglob('*') if p.is_file() and not any(x in {'.git','outputs','submission','__pycache__','.Xil','.qtcreator','.qtc_clangd'} for x in p.relative_to(ROOT).parts) and p.suffix.lower() not in {'.pyc','.exe','.dll','.o','.obj','.jou','.log','.mp4','.mov','.avi','.mkv','.wmv','.webm','.mpeg','.mpg','.m4v','.pdf','.doc','.docx','.ppt','.pptx','.zip','.rar'})
def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--rar',default='rar');a=ap.parse_args()
    out=ROOT/'submission';out.mkdir(exist_ok=True)
    manifest=ROOT/'docs/submission_manifest.csv'
    with manifest.open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f);w.writerow(['path','bytes','sha256'])
        for p in files():
            if p==manifest:continue
            w.writerow([p.relative_to(ROOT).as_posix(),p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest()])
    items=files();tmp=ROOT/'outputs/package';tmp.mkdir(parents=True,exist_ok=True)
    lst=tmp/'files.txt';lst.write_text('\n'.join(p.relative_to(ROOT).as_posix() for p in items),encoding='utf-8')
    archive=out/'agrivision_fpga.rar';fresh=tmp/'agrivision_fpga_new.rar'
    if fresh.exists():fresh.unlink()
    log=tmp/'rar.log'
    with log.open('w',encoding='utf-8') as f:
        subprocess.run([a.rar,'a','-ma5','-m5','-s','-idq','-scfl','-apagrivision_fpga',str(fresh),'@'+str(lst)],cwd=ROOT,check=True,stdout=f,stderr=subprocess.STDOUT)
    assert fresh.stat().st_size<200_000_000,'RAR exceeds upload limit'
    subprocess.run([a.rar,'t','-idq',str(fresh)],check=True,capture_output=True)
    # A fresh extraction directory prevents stale files from hiding packaging omissions.
    extract=Path(tempfile.mkdtemp(prefix='verify_',dir=tmp))
    subprocess.run([a.rar,'x','-idq',str(fresh),str(extract)+os.sep],check=True,capture_output=True)
    extracted=extract/'agrivision_fpga'
    expected={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in items}
    actual={p.relative_to(extracted).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in extracted.rglob('*') if p.is_file()}
    assert expected==actual,'Archive contents or hashes differ'
    fresh.replace(archive)
    result={'archive_bytes':archive.stat().st_size,'archive_mb_decimal':round(archive.stat().st_size/1e6,2),'files':len(items),'rar_integrity':'PASS','exact_extraction_set_and_hashes':'PASS','sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'code_only':True,'independent_submission_materials':False,'no_git_or_outputs':True}
    (out/'package_verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))
if __name__=='__main__':main()
