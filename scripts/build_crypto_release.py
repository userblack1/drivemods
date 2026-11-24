"""Build an audited, reproducible source ZIP of one clean Git commit.

No history, credentials, operational databases or browser profiles are copied.
Unlike the older field-kit builder, this includes the complete checkout package.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from scripts.handoff_audit import audit,indexed_blobs

REQUIRED=frozenset({
    'requirements.lock','requirements.dev.lock','README.md','HANDOFF.md',
    'crypto_checkout/application.py','crypto_checkout/bot.py','crypto_checkout/checkout.py',
    'crypto_checkout/runtime.py','crypto_checkout/store_adapter.py','crypto_checkout/binance.py',
    'crypto_checkout/ledger.py','crypto_checkout/tron.py','crypto_checkout/fulfillment.py',
    'drivemods_bot/config.py','drivemods_bot/supplier.py',
    'deploy/drivemods-checkout.service','deploy/crypto-checkout.env.example',
})


def git(root,*args):
    r=subprocess.run(['git','-C',str(root),*args],capture_output=True)
    if r.returncode:
        raise ValueError('Could not inspect Git; no source archive created')
    return r.stdout


def build(root,output):
    root=Path(root).resolve();output=Path(output).resolve()
    release=(root/'release').resolve()
    if ((root/'release').is_symlink() or release.parent!=root
            or output.parent!=release or output.suffix.lower()!='.zip'
            or output.exists() or output.with_suffix('.sha256').exists()):
        raise ValueError('Choose a new ZIP directly inside the release directory')
    if git(root,'status','--porcelain','--untracked-files=no').strip():
        raise ValueError('Commit the reviewed source changes before building a release')
    report=audit(root,history=True)
    if not report['ok']:
        raise ValueError('Source audit failed; no source archive created')
    blobs=indexed_blobs(root)
    if REQUIRED-set(blobs) or 'release-manifest.json' in blobs:
        raise ValueError('Complete checkout source and deployment configuration are required')
    for name in blobs:
        if name.startswith(('/', '\\')) or '..' in Path(name).parts:
            raise ValueError('Unsafe archive member')
    commit=git(root,'rev-parse','HEAD').decode('ascii').strip()
    tree=git(root,'rev-parse','HEAD^{tree}').decode('ascii').strip()
    manifest={'commit':commit,'tree':tree,'history_included':False,
              'secrets_included':False,'operational_data_included':False,
              'production_checkout_enabled':False,
              'files':{name:hashlib.sha256(b).hexdigest() for name,b in sorted(blobs.items())}}
    release.mkdir(exist_ok=True)
    fd,tempname=tempfile.mkstemp(prefix='.checkout-',suffix='.zip.part',dir=release)
    os.close(fd);pending=Path(tempname)
    try:
        with zipfile.ZipFile(pending,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
            entries={**blobs,'release-manifest.json':json.dumps(manifest,indent=2).encode('utf-8')}
            for name,data in sorted(entries.items()):
                item=zipfile.ZipInfo('DriveMods-checkout/'+name,date_time=(2026,1,1,0,0,0))
                item.compress_type=zipfile.ZIP_DEFLATED
                item.external_attr=0o100644<<16
                z.writestr(item,data)
        # A source release must never overwrite a previous reviewable artifact.
        os.link(pending,output)  # Atomic no-replace on this same filesystem.
        pending.unlink()
    finally:
        pending.unlink(missing_ok=True)
    with output.with_suffix('.sha256').open('x',encoding='ascii') as checksum:
        checksum.write(hashlib.sha256(output.read_bytes()).hexdigest()+'  '+output.name+'\n')
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    commit=git(ROOT,'rev-parse','--short=12','HEAD').decode('ascii').strip()
    try:
        result=build(ROOT,args.output or ROOT/'release'/('DriveMods-checkout-'+commit+'.zip'))
    except (ValueError,OSError):
        p.exit(1,'Release not built: require a clean audited complete source and a new destination.\n')
    print(result)


if __name__=='__main__':main()
