"""Release gates fail closed without touching data readers or publishing anything."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tools import check_release as gate


class ReleaseChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        names = ['VERSION', 'CHANGELOG.md', 'input_discovery.py',
                 'validation/protected_rmp_readers.json', 'tools/protected_discovery.json']
        audit = json.loads((gate.ROOT / 'validation/protected_rmp_readers.json').read_text('utf-8'))
        for name in names + list(audit['files']):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(gate.ROOT / name, target)
        self.root = root
        self.version = (root / 'VERSION').read_text('utf-8').strip()
        self.tag = 'v' + self.version
        self.root_patch = patch.object(gate, 'ROOT', root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_protected_implementation_change_is_rejected(self):
        with (self.root / 'rmp_l4.py').open('a', encoding='utf-8') as stream:
            stream.write('\n# unexpected change\n')
        with self.assertRaisesRegex(ValueError, 'Protected engine changed'):
            gate.check()

    def test_missing_changelog_version_is_rejected(self):
        (self.root / 'CHANGELOG.md').write_text('# empty\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'CHANGELOG'):
            gate.check()

    def test_pr_cannot_reuse_a_published_version(self):
        with patch.object(gate, 'git', return_value=self.tag):
            with self.assertRaisesRegex(ValueError, 'new VERSION'):
                gate.check(unreleased=True)

    def test_tag_must_match_version(self):
        with self.assertRaisesRegex(ValueError, 'Tag does not match'):
            gate.check(tag='v999.0.0')

    def test_tag_must_point_to_checked_out_commit(self):
        with patch.object(gate, 'git', side_effect=['a' * 40, 'b' * 40]):
            with self.assertRaisesRegex(ValueError, 'checked-out commit'):
                gate.check(tag=self.tag)

    def test_release_commit_must_belong_to_main(self):
        with patch.object(gate, 'git', return_value='a' * 40), patch.object(
                gate.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'git')):
            with self.assertRaises(subprocess.CalledProcessError):
                gate.check(tag=self.tag)

    def ci_check(self, runs):
        body = json.dumps({'workflow_runs': runs}).encode()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(gate, 'git', return_value='a' * 40))
            stack.enter_context(patch.object(gate.subprocess, 'run'))
            stack.enter_context(patch.dict(gate.os.environ, {
                'GITHUB_REPOSITORY': 'henrydoyle6/hlp-la-screen', 'GH_TOKEN': 'test-token'}))
            stack.enter_context(patch.object(gate.urllib.request, 'urlopen',
                                             return_value=io.BytesIO(body)))
            return gate.check(tag=self.tag, main_ci=True)

    @staticmethod
    def run_record(run_id=1, conclusion='success', branch='main'):
        return {'id': run_id, 'head_sha': 'a' * 40, 'head_branch': branch, 'event': 'push',
                'status': 'completed', 'conclusion': conclusion,
                'html_url': 'https://github.com/henrydoyle6/hlp-la-screen/actions/runs/1'}

    def test_successful_pr_or_other_branch_does_not_replace_main_ci(self):
        with self.assertRaisesRegex(ValueError, 'No main push CI'):
            self.ci_check([self.run_record(branch='feature')])

    def test_latest_main_failure_is_not_hidden_by_an_older_success(self):
        with self.assertRaisesRegex(ValueError, 'has not succeeded'):
            self.ci_check([self.run_record(1), self.run_record(2, 'failure')])

    def test_exact_successful_main_commit_passes(self):
        report = self.ci_check([self.run_record()])
        self.assertEqual(report['main_ci_run'], 1)
        self.assertEqual(report['commit'], 'a' * 40)
