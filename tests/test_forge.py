import os
import json
import time
import threading
import fcntl
from pathlib import Path
import subprocess
import tempfile
import shutil
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE = '''#!/usr/bin/env python3
import os, sys, pathlib, time
a=sys.argv[1:]
if '--help' in a:
 if os.environ.get('HELPS'):
  with open(os.environ['HELPS'], 'a') as f: f.write('help\\n')
 print('-p --model --add-dir --effort --permission-mode --allowedTools --disallowed-tools --dangerously-skip-permissions -m -C -o -c -s --skip-git-repo-check --approve-for-me --base --uncommitted --dangerously-bypass-approvals-and-sandbox --dir --variant --auto --agent --print --print-timeout --mode --json --output-format'); sys.exit()
p = sys.stdin.read() if '-p' in a or a[-1] == '-' else a[-1]
qa = '--disallowed-tools' in a or '-s' in a or 'review' in a
name = 'a.txt' if 'TASK_a' in p else ('b.txt' if 'TASK_b' in p else ('c.txt' if 'TASK_c' in p else 'change.txt'))
with open(os.environ['CALLS'], 'a') as f: f.write(('qa' if qa else 'dwarf')+'\\n')
if os.environ.get('EVENTS'):
 with open(os.environ['EVENTS'], 'a') as f: f.write(name+(' qa' if qa else ' dwarf')+' '+str(time.time())+'\\n')
if name == 'b.txt' and not qa and os.environ.get('SLOW_B'): time.sleep(4)
if not qa:
 if os.environ.get('SLEEP'): time.sleep(10)
 pathlib.Path(name).write_text(os.environ.get('CONTENT','implemented')+'\\n')
 if os.environ.get('STAGED'):
  import subprocess; subprocess.run(['git','add',name], check=True)
else:
 if os.environ.get('MUTATE'): pathlib.Path(name).write_text('tampered\\n')
 if os.environ.get('SOURCE'): pathlib.Path(os.environ['SOURCE'],'change.txt').write_text('source tampered\\n')
verdict = 'FAIL' if name == 'a.txt' and os.environ.get('FAIL_A') else os.environ.get('VERDICT','PASS')
result = ('FORGE_VERDICT: '+verdict) if qa else 'implemented'
if '-o' in a:
 pathlib.Path(a[a.index('-o')+1]).write_text(result)
 import json; print(json.dumps({'type':'turn.completed','usage':{'input_tokens':20,'cached_input_tokens':5,'output_tokens':3}}))
elif '--output-format' in a:
 import json; print(json.dumps({'type':'result', 'result':result, 'usage':{'input_tokens':10,'cache_read_input_tokens':2,'cache_creation_input_tokens':3,'output_tokens':4}}))
else: print(result)
'''

class ForgeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name); self.repo=self.root/'repo'; self.repo.mkdir()
  self.bin=self.root/'bin'; self.bin.mkdir()
  for cli in ['codex','claude']:
   p=self.bin/cli; p.write_text(FAKE); p.chmod(0o755)
  for name in 'bash python3 git tar awk sed grep tr sort wc head tail cat mkdir rm mv cp touch date basename dirname mktemp xargs tee sleep pkill cmp'.split():
   binary=shutil.which(name)
   if binary: (self.bin/name).symlink_to(binary)
  self.env=dict(os.environ, PATH=str(self.bin), FORGE_MEMORY='off', FORGE_TIMEOUT='0', CALLS=str(self.root/'calls'), GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@test', GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@test')
  self.git('init','-q'); (self.repo/'base.txt').write_text('base\n'); self.git('add','.'); self.git('commit','-qm','initial')
 def git(self,*args):
  return subprocess.check_output(['git',*args],cwd=self.repo,env=self.env)
 def run_script(self,name,*args):
  return subprocess.run(['bash',str(ROOT/'scripts'/name),*map(str,args)],cwd=self.repo,env=self.env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 def solo(self,*args):
  run=self.root/'solo'; run.mkdir(exist_ok=True); (run/'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
  return self.run_script('forge-solo.sh',run,'--repo',self.repo,'--dwarf','sol',*args)
 def plan(self,deps='a',files=('a.txt','b.txt')):
  p=self.root/'plan'; p.mkdir()
  (p/'tasks.tsv').write_text(f'a\t-\tlow\t{files[0]}\tsol\topus\tA\nb\t{deps}\tlow\t{files[1]}\tsol\topus\tB\n')
  for id in ['a','b']:
   t=p/'tasks'/id; t.mkdir(parents=True); (t/'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_'+id)
  r=self.run_script('forge-parallel.sh','plan',p,'--repo',self.repo,'--no-memory'); self.assertEqual(r.returncode,0,r.stdout)
  return p
 def test_staged_capture(self):
  self.env['STAGED']='1'; r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  self.assertIn('implemented',(self.root/'solo/changes.diff').read_text())
 def test_failed_dependency_blocks_dispatch(self):
  p=self.plan(); self.env['FAIL_A']='1'; self.run_script('forge-parallel.sh','run',p)
  self.assertEqual((p/'tasks/b/status').read_text().strip(),'BLOCKED')
  self.assertFalse((p/'tasks/b/dwarf.last').exists())

 def test_existing_work_and_index_preserved(self):
  (self.repo/'base.txt').write_text('user staged\n'); self.git('add','base.txt')
  index=self.git('diff','--cached','--binary')
  (self.repo/'old untracked.txt').write_text('user untracked\n')
  r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  self.assertEqual(index,self.git('diff','--cached','--binary'))
  diff=(self.root/'solo/changes.diff').read_text()
  self.assertNotIn('user staged',diff); self.assertNotIn('user untracked',diff)
  self.assertIn('user staged',(self.root/'solo/existing.diff').read_text())
 def test_overlap_directories(self):
  p=self.plan('-',('./src/','src/child.py'))
  self.assertEqual((p/'waves.tsv').read_text(),'1\ta\n2\tb\n')
 def test_review_mutation_invalidated(self):
  self.env['MUTATE']='1'; r=self.solo(); self.assertEqual(r.returncode,5,r.stdout)
  self.assertEqual((self.root/'solo/verdict').read_text().strip(),'INVALIDATED')
  self.assertEqual((self.repo/'change.txt').read_text(),'implemented\n')
 def test_source_mutation_invalidated(self):
  self.env['SOURCE']=str(self.repo); r=self.solo(); self.assertEqual(r.returncode,5,r.stdout)
 def test_complete_requirements(self):
  r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  self.assertIn('REQUIREMENT_SENTINEL',(self.root/'solo/qa.input').read_text())
 def test_unavailable_qa_preflight(self):
  r=self.solo('--qa','opus::openclaude')
  self.assertNotEqual(r.returncode,0,r.stdout)
  self.assertFalse((self.root/'calls').exists())
 def test_missing_required_flag(self):
  cli=self.bin/'claude'; cli.write_text('#!/bin/sh\necho --model\n'); cli.chmod(0o755)
  r=self.solo(); self.assertEqual(r.returncode,3,r.stdout)
  self.assertFalse((self.root/'calls').exists())
 def test_bad_effort(self):
  r=self.run_script('forge-dispatch.sh','doctor','--spec','literal:typo:codex')
  self.assertEqual(r.returncode,2,r.stdout); self.assertFalse((self.root/'calls').exists())
 def test_verdict_requires_final_standalone_line(self):
  self.env['VERDICT']='PASS\nprose'; r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  self.assertEqual((self.root/'solo/verdict').read_text().strip(),'UNKNOWN')
 def test_timeout(self):
  self.env['SLEEP']='1'; r=self.solo('--timeout','1'); self.assertEqual(r.returncode,7,r.stdout)
 def test_retry_rechecks_dependencies(self):
  p=self.plan(); self.env['VERDICT']='FAIL'; self.run_script('forge-parallel.sh','run',p)
  before=(self.root/'calls').read_text()
  r=self.run_script('forge-parallel.sh','retry',p,'b'); self.assertEqual(r.returncode,5,r.stdout)
  self.assertEqual(before,(self.root/'calls').read_text())
 def test_failed_integration_check_blocks_user_merge(self):
  p=self.plan(); parent=self.git('rev-parse','HEAD')
  r=self.run_script('forge-parallel.sh','run',p,'--verify','echo check-output; exit 9')
  self.assertEqual(r.returncode,5,r.stdout)
  self.assertEqual((p/'verification.exit').read_text().strip(),'9')
  self.assertIn('check-output',(p/'verification.log').read_text())
  r=self.run_script('forge-parallel.sh','integrate',p,'--approved')
  self.assertEqual(r.returncode,5,r.stdout); self.assertEqual(parent,self.git('rev-parse','HEAD'))
 def test_parallel_review_mutation_not_merged(self):
  p=self.plan(); self.env['MUTATE']='1'; self.run_script('forge-parallel.sh','run',p)
  self.assertEqual((p/'tasks/a/status').read_text().strip(),'INVALIDATED')
  self.assertFalse((p/'tasks/a/merged').exists())
 def test_preflight_all_tasks_before_dispatch(self):
  p=self.plan(); t=p/'tasks.tsv'; t.write_text(t.read_text().replace('b\ta\tlow\tb.txt\tsol\topus', 'b\ta\tlow\tb.txt\tsol\topus::openclaude'))
  r=self.run_script('forge-parallel.sh','run',p); self.assertNotEqual(r.returncode,0,r.stdout)
  self.assertFalse((self.root/'calls').exists())

 def test_successful_dependencies_and_integration(self):
  p=self.plan(); r=self.run_script('forge-parallel.sh','run',p,'--verify','test -f a.txt && test -f b.txt')
  self.assertEqual(r.returncode,0,r.stdout)
  self.assertEqual((p/'verification.status').read_text().strip(),'PASS')
  self.assertIn('REQUIREMENT_SENTINEL',(p/'tasks/a/qa.input').read_text())
  self.assertIn("Run the task's requested verification",(p/'tasks/a/dwarf.input').read_text())
  self.assertIn('MERGED',(p/'results.tsv').read_text())
  r=self.run_script('forge-parallel.sh','integrate',p,'--approved')
  self.assertEqual(r.returncode,0,r.stdout); self.assertTrue((self.repo/'b.txt').exists())
 def test_retry_then_unblock(self):
  p=self.plan(); self.env['VERDICT']='FAIL'; self.run_script('forge-parallel.sh','run',p)
  self.env['VERDICT']='PASS'; self.env['CONTENT']='fixed'
  r=self.run_script('forge-parallel.sh','retry',p,'a'); self.assertEqual(r.returncode,0,r.stdout)
  r=self.run_script('forge-parallel.sh','retry',p,'b'); self.assertEqual(r.returncode,0,r.stdout)
  self.assertTrue((p/'tasks/b/merged').exists())
  self.assertIn('previous attempt was reviewed',(p/'tasks/a/dwarf.input').read_text())
 def test_unchanged_retry_rejected(self):
  p=self.plan(); self.env['VERDICT']='FAIL'; self.run_script('forge-parallel.sh','run',p)
  before=(self.root/'calls').read_text().count('qa')
  self.env['VERDICT']='PASS'; r=self.run_script('forge-parallel.sh','retry',p,'a')
  self.assertEqual(r.returncode,5,r.stdout)
  self.assertEqual(before,(self.root/'calls').read_text().count('qa'))
 def test_integration_branch_mutation_rejected(self):
  p=self.plan(); self.run_script('forge-parallel.sh','run',p)
  wt=Path((p/'wt_root').read_text().strip())/'_integration'
  subprocess.run(['git','commit','--allow-empty','-qm','unreviewed'],cwd=wt,env=self.env,check=True)
  r=self.run_script('forge-parallel.sh','integrate',p,'--approved'); self.assertEqual(r.returncode,3,r.stdout)
 def test_default_timeout_returns_promptly(self):
  self.env.pop('FORGE_TIMEOUT'); r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)

 def test_export_ignore_does_not_hide_review_files(self):
  (self.repo/'.gitattributes').write_text('base.txt export-ignore\n')
  self.git('add','.gitattributes'); self.git('commit','-qm','attributes')
  r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  review=next((self.root/'solo').glob('review-*'))
  self.assertTrue((review/'base.txt').exists())
 def test_verification_mutation_blocks_integration(self):
  p=self.plan(); parent=self.git('rev-parse','HEAD')
  r=self.run_script('forge-parallel.sh','run',p,'--verify','echo corrupt > a.txt')
  self.assertEqual(r.returncode,5,r.stdout)
  r=self.run_script('forge-parallel.sh','integrate',p,'--approved')
  self.assertEqual(r.returncode,5,r.stdout); self.assertEqual(parent,self.git('rev-parse','HEAD'))
 def test_native_review_informational(self):
  r=self.solo('--qa','sol','--native-review'); self.assertEqual(r.returncode,0,r.stdout)
  self.assertEqual((self.root/'solo/verdict').read_text().strip(),'UNKNOWN')
 def test_native_review_wrong_harness_rejected_before_dispatch(self):
  r=self.solo('--native-review'); self.assertEqual(r.returncode,2,r.stdout)
  self.assertFalse((self.root/'calls').exists())
 def test_routing_and_clamping(self):
  for name in ['openclaude','opencode','agy']:
   cli=self.bin/name; cli.write_text(FAKE); cli.chmod(0o755)
  for spec,model,effort in [('sol','gpt-5.6-sol','medium'),('sol:ultra:openclaude','gpt-5.6-sol','max'),('gemini-pro:medium','gemini-3.1-pro','low'),('opus::antigravity','claude-opus-4-6-thinking','<harness default>'),('grok::opencode','github-copilot/grok-4.6','<harness default>')]:
   with self.subTest(spec=spec):
    r=self.run_script('forge-dispatch.sh','doctor','--spec',spec)
    self.assertEqual(r.returncode,0,r.stdout)
    self.assertIn('model='+model,r.stdout); self.assertIn('effort='+effort,r.stdout)
  self.assertFalse((self.root/'calls').exists())

 def test_force_staged_ignored_file_captured(self):
  (self.repo/'.gitignore').write_text('change.txt\n')
  self.git('add','.gitignore'); self.git('commit','-qm','ignore')
  cli=self.bin/'codex'; cli.write_text(FAKE.replace("['git','add',name]", "['git','add','-f',name]")); cli.chmod(0o755)
  self.env['STAGED']='1'; r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  self.assertIn('implemented',(self.root/'solo/changes.diff').read_text())


 def test_large_unicode_prompt_stdin_fifo(self):
  payload=('要求 café 😀\n'*20000).encode()
  for mode in ('file','stdin','fifo'):
   run=self.root/mode; run.mkdir(); source=self.root/(mode+'.input')
   args=['bash',str(ROOT/'scripts/forge-dispatch.sh'),'dwarf','sol','--repo',str(self.repo),'--run-dir',str(run),'--output','summary','--prompt-file']
   if mode == 'fifo':
    os.mkfifo(source)
    writer=threading.Thread(target=lambda: source.write_bytes(payload)); writer.start()
   elif mode == 'file': source.write_bytes(payload)
   args.append('-' if mode == 'stdin' else str(source))
   result=subprocess.run(args,input=payload if mode == 'stdin' else None,env=self.env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=8)
   if mode == 'fifo': writer.join(timeout=1)
   self.assertEqual(result.returncode,0,result.stdout)
   self.assertEqual((run/'dwarf.prompt').read_bytes(),payload)
   self.assertNotIn('要求',(run/'dwarf.cmd').read_text())
 def test_whitespace_rejected(self):
  run=self.root/'empty'; run.mkdir(); prompt=self.root/'whitespace'; prompt.write_text(' \t\r\n\v\f'*2000)
  r=self.run_script('forge-dispatch.sh','dwarf','sol','--repo',self.repo,'--run-dir',run,'--prompt-file',prompt)
  self.assertEqual(r.returncode,2,r.stdout)
  metrics=json.loads(next(run.glob('attempts/*/metrics.json')).read_text())
  self.assertEqual(metrics['exit_code'],2); self.assertIsNone(metrics['input_tokens'])
 def test_summary_and_native_usage(self):
  r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  self.assertNotIn('turn.completed',r.stdout)
  run=self.root/'solo'
  report=self.run_script('forge-dispatch.sh','report',run)
  records=json.loads(report.stdout)['attempts']
  dwarf=next(x for x in records if x['role']=='dwarf')
  qa=next(x for x in records if x['role']=='qa')
  self.assertEqual(dwarf['input_tokens'],20); self.assertEqual(dwarf['cached_input_tokens'],5)
  self.assertEqual(qa['input_tokens'],15)
  self.assertEqual((run/'qa.last').read_text().strip(),'FORGE_VERDICT: PASS')
  self.assertIn('turn.completed',(run/'dwarf.log').read_text())
  self.assertTrue(any(x['role']=='solo' and x['elapsed_s']['snapshot']>0 for x in records))
 def test_full_output_and_attempt_history(self):
  r=self.solo('--output','full'); self.assertEqual(r.returncode,0,r.stdout); self.assertIn('turn.completed',r.stdout)
  self.env['CONTENT']='second'; r=self.solo()
  records=list((self.root/'solo').glob('attempts/dwarf-*/dwarf.last'))
  self.assertEqual(len(records),2)
 def test_context_once(self):
  run=self.root/'solo'; run.mkdir(); approach=self.root/'approach'; approach.write_text('APPROACH_SENTINEL\n')
  r=self.solo('--approach',approach); self.assertEqual(r.returncode,0,r.stdout)
  qa=(run/'qa.input').read_text()
  self.assertEqual(qa.count('REQUIREMENT_SENTINEL'),1); self.assertEqual(qa.count('APPROACH_SENTINEL'),1)
  self.assertNotIn('Edit the files in this repository directly',qa)
 def test_help_cache_shared_and_invalidated(self):
  self.env['HELPS']=str(self.root/'helps'); p=self.plan()
  r=self.run_script('forge-parallel.sh','run',p); self.assertEqual(r.returncode,0,r.stdout)
  self.assertEqual((self.root/'helps').read_text().count('help'),2)
  self.env['FORGE_CAPABILITY_CACHE']=str(p/'capabilities')
  cli=self.bin/'codex'; cli.write_text(cli.read_text()+'\n# changed executable\n')
  r=self.run_script('forge-dispatch.sh','doctor','--spec','sol'); self.assertEqual(r.returncode,0,r.stdout)
  self.assertEqual((self.root/'helps').read_text().count('help'),3)
 def test_dependent_starts_before_unrelated_task_finishes(self):
  p=self.plan('-')
  with (p/'tasks.tsv').open('a') as f: f.write('c\ta\tlow\tc.txt\tsol\topus\tC\n')
  t=p/'tasks/c'; t.mkdir(); (t/'prompt.md').write_text('Implement TASK_c')
  self.env['EVENTS']=str(self.root/'events'); self.env['SLOW_B']='1'
  r=self.run_script('forge-parallel.sh','run',p,'--max-parallel','2'); self.assertEqual(r.returncode,0,r.stdout)
  events=(self.root/'events').read_text().splitlines()
  c=next(float(x.split()[-1]) for x in events if x.startswith('c.txt dwarf'))
  b=next(float(x.split()[-1]) for x in events if x.startswith('b.txt qa'))
  self.assertLess(c,b,events)
  self.assertNotIn('b              low     MERGED',(t/'baseline.capsule').read_text())
 def test_resume_does_not_retry_failed_tasks(self):
  p=self.plan(); self.env['FAIL_A']='1'; self.run_script('forge-parallel.sh','run',p)
  before=(self.root/'calls').read_text()
  self.run_script('forge-parallel.sh','run',p)
  self.assertEqual(before,(self.root/'calls').read_text())
 def test_snapshot_modes_symlinks_deletions_independent(self):
  (self.repo/'executable').write_text('#!/bin/sh\n'); (self.repo/'executable').chmod(0o755)
  (self.repo/'link').symlink_to('base.txt')
  self.git('add','.'); self.git('commit','-qm','modes')
  r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  review=next((self.root/'solo').glob('review-*'))
  self.assertTrue((review/'link').is_symlink()); self.assertTrue((review/'executable').stat().st_mode & 0o111)
  self.assertFalse((review/'.git/objects/info/alternates').exists())
  subprocess.run(['git','fsck','--full'],cwd=review,env=self.env,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)

 def test_prompt_alias_does_not_truncate(self):
  run=self.root/'alias'; run.mkdir(); prompt=run/'dwarf.prompt'; prompt.write_bytes(b'EXACT\r\n\n')
  alias=self.root/'prompt-alias'; alias.symlink_to(prompt)
  r=self.run_script('forge-dispatch.sh','dwarf','sol','--repo',self.repo,'--run-dir',run,'--prompt-file',alias)
  self.assertEqual(r.returncode,0,r.stdout); self.assertEqual(prompt.read_bytes(),b'EXACT\r\n\n')
 def test_zero_capacity_rejected(self):
  p=self.plan(); r=self.run_script('forge-parallel.sh','run',p,'--max-parallel','00')
  self.assertEqual(r.returncode,2,r.stdout); self.assertFalse((self.root/'calls').exists())
 def test_whitespace_locale_equivalence(self):
  for value in (' ', '\t', '\r', '\n', '\v', '\f', '\u00a0', '\u2003', '\u2028', '😀'):
   source=self.root/'space'; source.write_text(value)
   old=subprocess.run(['/bin/bash','-c','p="$(cat "$1")"; [ -n "${p//[[:space:]]/}" ]','check',str(source)],env=self.env)
   new=subprocess.run(['/bin/bash','-c',"grep -q '[^[:space:]]' \"$1\"",'check',str(source)],env=self.env)
   self.assertEqual(old.returncode==0,new.returncode==0,repr(value))

 def test_active_directory_overlap_serialized(self):
  p=self.plan('-',('src','src/child.py')); self.env['EVENTS']=str(self.root/'events')
  r=self.run_script('forge-parallel.sh','run',p,'--max-parallel','2'); self.assertEqual(r.returncode,0,r.stdout)
  events=(self.root/'events').read_text().splitlines()
  a=next(float(x.split()[-1]) for x in events if x.startswith('a.txt qa'))
  b=next(float(x.split()[-1]) for x in events if x.startswith('b.txt dwarf'))
  self.assertLess(a,b,events)
 def test_concurrent_plan_operation_rejected_before_dispatch(self):
  p=self.plan()
  with (p/'schedule.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
   r=self.run_script('forge-parallel.sh','run',p)
  self.assertEqual(r.returncode,3,r.stdout); self.assertFalse((self.root/'calls').exists())
 def test_missing_prompt_does_not_archive_stale_input(self):
  r=self.solo(); self.assertEqual(r.returncode,0,r.stdout)
  run=self.root/'solo'
  r=self.run_script('forge-dispatch.sh','dwarf','sol','--repo',self.repo,'--run-dir',run,'--prompt-file',self.root/'missing')
  self.assertEqual(r.returncode,2,r.stdout)
  records=[json.loads(p.read_text()) for p in run.glob('attempts/dwarf-*/metrics.json')]
  failed=next(x for x in records if x['exit_code']==2)
  self.assertIsNone(failed['prompt_bytes']);self.assertIsNone(failed['input_tokens'])

if __name__=='__main__': unittest.main()
