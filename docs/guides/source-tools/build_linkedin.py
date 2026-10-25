"""Build the li executable using Cargo metadata rather than guessed artifact paths."""
import json
import pathlib
import shutil
import subprocess

root = pathlib.Path('/src/linkedin-rs')
manifest = next((p for p in (root/'Cargo.toml', root/'linkedin/Cargo.toml') if p.is_file()), None)
if manifest is None:
    raise SystemExit('linkedin-rs checkout has no expected Cargo workspace; inspect the selected source revision')
meta = json.loads(subprocess.check_output(['cargo','metadata','--no-deps','--format-version','1','--manifest-path',str(manifest)]))
choices = [(p,t) for p in meta['packages'] for t in p['targets'] if 'bin' in t['kind'] and t['name'] in {'li','linkedin-cli'}]
if len(choices) != 1:
    raise SystemExit('Expected one li/linkedin-cli Cargo binary; upstream structure changed')
p, target = choices[0]
command = ['cargo','build','--locked','--release','--manifest-path',str(manifest),'-p',p['name'],'--bin',target['name']]
subprocess.run(command,check=True)
exe = pathlib.Path(meta['target_directory'])/'release'/target['name']
if not exe.is_file(): raise SystemExit('Cargo did not produce the declared binary')
pathlib.Path('/out').mkdir(exist_ok=True)
shutil.copy2(exe,'/out/li')
revision = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
pathlib.Path('/out/linkedin-rs-revision.txt').write_text(revision+'\n')
subprocess.run(['/out/li','--help'],check=True,stdout=subprocess.DEVNULL)
