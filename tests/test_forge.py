import os
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
 print('-p --model --add-dir --effort --permission-mode --allowedTools --disallowed-tools --dangerously-skip-permissions -m -C -o -c -s --skip-git-repo-check --approve-for-me --base --uncommitted --dangerously-bypass-approvals-and-sandbox --dir --variant --auto --agent --print --print-timeout --mode'); sys.exit()
p = sys.stdin.read() if '-p' in a else a[-1]
qa = '--disallowed-tools' in a or '-s' in a or 'review' in a
name = 'a.txt' if 'TASK_a' in p else ('b.txt' if 'TASK_b' in p else 'change.txt')
with open(os.environ['CALLS'], 'a') as f: f.write(('qa' if qa else 'dwarf')+'\\n')
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
if '-o' in a: pathlib.Path(a[a.index('-o')+1]).write_text(result)
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

if __name__=='__main__': unittest.main()
