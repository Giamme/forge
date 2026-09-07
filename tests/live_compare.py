#!/usr/bin/env python3
"""Explicitly authorized 24-dispatch comparison. Not part of offline discovery.

Usage: python3 tests/live_compare.py /absolute/artifact/directory --execute
The exclusive ledger counts failed attempts too. Never resets or retries a dispatch.
"""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
DWARF = 'sol:medium:codex'
QA = 'terra:xhigh:codex'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args]).decode().strip()


def init_repo(path, files):
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    for name, content in files.items():
        dest = path / name; dest.parent.mkdir(parents=True, exist_ok=True); dest.write_text(content)
    git(path, 'add', '.')
    git(path, '-c', 'user.name=forge-bench', '-c', 'user.email=bench@local', 'commit', '-qm', 'fixture')


def run(command, env, out):
    started = time.monotonic()
    with out.open('w') as log:
        result = subprocess.run(list(map(str, command)), env=env, stdout=log, stderr=subprocess.STDOUT)
    return {'exit_code': result.returncode, 'elapsed_s': time.monotonic() - started, 'log': str(out)}


FIXTURES = [
    ('slug', 'Implement slug(text) in slug.py. Use unicodedata.normalize NFKD then ASCII encoding with ignore, lowercase, replace each run of non-ASCII-alphanumeric characters with one hyphen, strip leading/trailing hyphens. Empty input returns empty. Do not change acceptance.py. Run python3 -B acceptance.py.',
     {'slug.py': 'def slug(text):\n    raise NotImplementedError\n',
      'acceptance.py': "from slug import slug\nassert slug('Crème brûlée!') == 'creme-brulee'\nassert slug(' A___B / C ') == 'a-b-c'\nassert slug('') == ''\nassert slug('😀') == ''\nassert slug('x123') == 'x123'\nprint('ACCEPTED')\n"}),
    ('ranges', 'Implement compress(values) in ranges.py. Return sorted inclusive integer interval tuples for unique input integers; consecutive integers form one interval. Accept any iterable, do not mutate inputs, empty returns []. Do not change acceptance.py. Run python3 -B acceptance.py.',
     {'ranges.py': 'def compress(values):\n    raise NotImplementedError\n',
      'acceptance.py': "from ranges import compress\na=[3,1,2,2,8,-2,-1]\nassert compress(a)==[(-2,-1),(1,3),(8,8)]\nassert a==[3,1,2,2,8,-2,-1]\nassert compress(iter([9,10]))==[(9,10)]\nassert compress([])==[]\nassert compress([0])==[(0,0)]\nprint('ACCEPTED')\n"})]


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('directory', type=Path)
    parser.add_argument('--execute', action='store_true'); args = parser.parse_args()
    if not args.execute: parser.error('--execute is required: this consumes live model quota')
    root = args.directory.resolve(); root.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidentally restarting the suite and paying twice.
    with (root / 'started').open('x') as file: file.write(str(time.time()))
    baseline = root / 'baseline'; baseline.mkdir()
    archive = subprocess.check_output(['git', '-C', str(ROOT), 'archive', 'f436210'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(baseline, filter='data')
    candidate = root / 'candidate'
    shutil.copytree(ROOT, candidate, ignore=shutil.ignore_patterns('.git', '__pycache__'))
    real = shutil.which('codex')
    if not real: raise RuntimeError('codex unavailable')
    bindir = root / 'bin'; bindir.mkdir()
    wrapper = bindir / 'codex'
    wrapper.write_text('''#!/usr/bin/env python3
import fcntl, json, os, sys, time
args=sys.argv[1:]
if '--help' not in args:
 with open(os.environ['BENCH_LEDGER'], 'a+') as f:
  fcntl.flock(f, fcntl.LOCK_EX); f.seek(0); rows=f.readlines()
  if len(rows)>=24: sys.exit('hard cap: 24 model dispatches')
  f.write(json.dumps({'number':len(rows)+1,'time':time.time(),'case':os.environ['BENCH_CASE'],'argv':args})+'\\n'); f.flush(); os.fsync(f.fileno())
 # Same event-output instrumentation on both revisions; no model/effort changes.
 if '--json' not in args: args.insert(1,'--json')
os.execv(os.environ['BENCH_CODEX'], [os.environ['BENCH_CODEX'], *args])
'''); wrapper.chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
               BENCH_LEDGER=str(root / 'dispatches.jsonl'), BENCH_CODEX=real,
               FORGE_MEMORY='off', FORGE_TIMEOUT='180', PYTHONDONTWRITEBYTECODE='1')
    results = []
    def save():
        (root / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    for name, prompt, files in FIXTURES:
        for label, source in [('baseline', baseline), ('candidate', candidate)]:
            case = name + '-' + label; env['BENCH_CASE'] = case
            work = root / case; work.mkdir(); repo = work / 'repo'; init_repo(repo, files)
            artifacts = work / 'run'; artifacts.mkdir(); (artifacts / 'prompt.md').write_text(prompt + '\n')
            (artifacts / 'approach.md').write_text('Keep the implementation small and pure. Preserve the requested contract and verify every edge case in acceptance.py.\n')
            print('START ' + case, flush=True)
            result = run(['bash', source / 'scripts/forge-solo.sh', artifacts, '--repo', repo,
                          '--dwarf', DWARF, '--qa', QA, '--no-memory', '--approach', artifacts / 'approach.md'], env, work / 'runner.log')
            result.update(case=case, artifacts=str(artifacts))
            result['acceptance'] = run(['python3', '-B', repo / 'acceptance.py'], env, work / 'acceptance.log')
            result['acceptance_unchanged'] = (repo / 'acceptance.py').read_text() == files['acceptance.py']
            result['verdict'] = (artifacts / 'verdict').read_text().strip() if (artifacts / 'verdict').exists() else 'MISSING'
            results.append(result); save(); print('DONE ' + case + ' ' + str(result['exit_code']), flush=True)
    files = {'parse.py': 'def parse(text):\n    raise NotImplementedError\n',
             'formatting.py': 'def format_total(value):\n    raise NotImplementedError\n',
             'pipeline.py': 'def total(text):\n    raise NotImplementedError\n',
             'acceptance.py': "from parse import parse\nfrom formatting import format_total\nfrom pipeline import total\nassert parse(' 1, -2,3 ')==[1,-2,3]\nassert parse('')==[]\ntry: parse('1,,2'); assert False\nexcept ValueError: pass\nassert format_total(-3)=='total=-3'\nassert total('1,2,-5')=='total=-2'\nassert total('')=='total=0'\nprint('ACCEPTED')\n"}
    tasks = [('a','-','parse.py','Implement parse(text): parse comma-separated integers with whitespace; empty or whitespace-only gives []; reject empty interior tokens with ValueError.'),
             ('b','-','formatting.py','Implement format_total(value): return the string total=<value> for an integer including zero or negatives.'),
             ('c','a,b','pipeline.py','Implement total(text) by importing parse from parse.py and format_total from formatting.py; return format_total(sum(parse(text))).')]
    for label, source in [('baseline', baseline), ('candidate', candidate)]:
        case='parallel-'+label; env['BENCH_CASE']=case; work=root/case; work.mkdir(); repo=work/'repo'; init_repo(repo,files)
        plan=work/'plan'; plan.mkdir(); (plan/'run_id').write_text(case)
        (plan/'goal.txt').write_text('Build the integer aggregation pipeline and pass python3 -B acceptance.py without editing acceptance.py.\n')
        (plan/'tasks.tsv').write_text(''.join(f'{id}\t{deps}\tlow\t{file}\t{DWARF}\t{QA}\t{id}\n' for id,deps,file,prompt in tasks))
        for id,deps,file,prompt in tasks:
            task=plan/'tasks'/id; task.mkdir(parents=True)
            (task/'prompt.md').write_text(prompt+' Edit only '+file+'. Verify with focused Python checks. Do not edit acceptance.py.\n')
            (task/'approach.md').write_text('Use a pure function and standard library only. Keep the exact function name and contract.\n')
        planned=run(['bash',source/'scripts/forge-parallel.sh','plan',plan,'--repo',repo,'--no-memory'],env,work/'plan.log')
        if planned['exit_code']: raise RuntimeError('plan failed: '+str(work/'plan.log'))
        print('START '+case,flush=True)
        result=run(['bash',source/'scripts/forge-parallel.sh','run',plan,'--max-parallel','2','--verify','python3 -B acceptance.py'],env,work/'runner.log')
        result.update(case=case,artifacts=str(plan))
        integration=work/'.forge-worktrees'/case/'_integration'
        result['acceptance']=run(['python3','-B',integration/'acceptance.py'],env,work/'acceptance.log')
        result['acceptance_unchanged']=(integration/'acceptance.py').read_text()==files['acceptance.py']
        result['tasks']=(plan/'results.tsv').read_text() if (plan/'results.tsv').exists() else ''
        results.append(result); save(); print('DONE '+case+' '+str(result['exit_code']),flush=True)
    for buggy in (False,True):
        for label,source in [('baseline',baseline),('candidate',candidate)]:
            case=('qa-buggy-' if buggy else 'qa-clean-')+label; env['BENCH_CASE']=case
            work=root/case; work.mkdir(); repo=work/'repo'; init_repo(repo,{'divide.py':'def average(values):\n    raise NotImplementedError\n'})
            base=git(repo,'rev-parse','HEAD')
            (repo/'divide.py').write_text('def average(values):\n    values = list(values)\n'+('' if buggy else '    if not values:\n        return None\n')+'    return sum(values) / len(values)\n')
            artifacts=work/'run';artifacts.mkdir()
            diff=git(repo,'diff','--binary')
            prompt='Review this complete diff against the requirements: average(values) accepts any iterable of numbers, returns the arithmetic mean, and returns None for empty input. Report confirmed correctness bugs only. Do not edit files. End with exactly FORGE_VERDICT: PASS or FORGE_VERDICT: FAIL.\n```diff\n'+diff+'\n```\n'
            (artifacts/'qa.input').write_text(prompt)
            # The benchmark exercises each revision's snapshot constructor as well.
            snapshot=artifacts/'review'
            snapshot_result=run(['bash','-c','source "$1"; tree="$(forge_tree "$2")"; forge_review_snapshot "$2" "$3" "$tree" "$4"','bench',source/'scripts/forge-artifact.sh',repo,base,snapshot],env,work/'snapshot.log')
            if snapshot_result['exit_code']: raise RuntimeError('snapshot failed')
            print('START '+case,flush=True)
            result=run(['bash',source/'scripts/forge-dispatch.sh','qa',QA,'--repo',snapshot,'--run-dir',artifacts,'--prompt-file',artifacts/'qa.input'],env,work/'runner.log')
            result.update(case=case,artifacts=str(artifacts),snapshot=snapshot_result)
            last=artifacts/'qa.last'; verdict=last.read_text().strip().splitlines()[-1] if last.exists() and last.stat().st_size else 'MISSING'
            result.update(verdict=verdict,expected='FORGE_VERDICT: FAIL' if buggy else 'FORGE_VERDICT: PASS')
            results.append(result);save();print('DONE '+case+' '+str(result['exit_code']),flush=True)
    print('Results: '+str(root/'results.json'),flush=True)


if __name__ == '__main__': main()
