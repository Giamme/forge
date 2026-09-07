#!/usr/bin/env python3
"""Quota-free prompt validation and snapshot benchmarks against a baseline copy."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def timed(command, timeout=30):
    start = time.perf_counter()
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return {'elapsed_s': time.perf_counter()-start, 'exit_code': result.returncode,
                'stdout': result.stdout.decode(errors='replace')}
    except subprocess.TimeoutExpired:
        return {'elapsed_s': time.perf_counter()-start, 'timeout': True}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('baseline', type=Path); parser.add_argument('output', type=Path)
    args = parser.parse_args(); results = {}
    with tempfile.TemporaryDirectory(prefix='forge-offline-bench-') as tmp:
        root=Path(tmp); prompt=root/'prompt'
        for size in (5000, 10000):
            prompt.write_text(('word '*((size+4)//5))[:size])
            results[str(size)] = {
                'baseline': timed(['/bin/bash','-c','PROMPT="$(cat "$1")"; [ -n "${PROMPT//[[:space:]]/}" ]','bench',str(prompt)],25),
                'candidate': timed(['/bin/bash','-c',"grep -q '[^[:space:]]' \"$1\"",'bench',str(prompt)])}
        repo=root/'repo'; repo.mkdir()
        def git(*argv):
            return subprocess.check_output(['git','-C',str(repo),*argv]).decode().strip()
        git('init','-q')
        for n in range(2000): (repo/f'{n}.txt').write_text('file '+str(n)+'\n')
        git('add','.'); git('-c','user.name=bench','-c','user.email=bench@local','commit','-qm','baseline')
        base=git('rev-parse','HEAD')
        (repo/'1.txt').unlink(); (repo/'2.txt').write_text('changed\n'); (repo/'added').symlink_to('3.txt')
        git('add','-A'); tree=git('write-tree')
        results['snapshot']={}
        for label,source in [('baseline',args.baseline),('candidate',ROOT)]:
            dest=root/label/'review'; dest.parent.mkdir()
            result=timed(['/bin/bash','-c','source "$1"; forge_review_snapshot "$2" "$3" "$4" "$5"','bench',str(source/'scripts/forge-artifact.sh'),str(repo),base,tree,str(dest)])
            result['tree']=subprocess.check_output(['git','-C',str(dest),'rev-parse','HEAD^{tree}']).decode().strip()
            result['equivalent']=result['tree']==tree
            results['snapshot'][label]=result
    args.output.write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))


if __name__=='__main__': main()
