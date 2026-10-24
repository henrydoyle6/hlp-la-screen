"""Offline release/engine checks; optional GitHub gate for the exact main commit."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def check(tag=None, main_ci=False, unreleased=False):
    version = (ROOT / 'VERSION').read_text('utf-8').strip()
    require(SEMVER.fullmatch(version), 'VERSION must be X.Y.Z without a v prefix')
    changelog = (ROOT / 'CHANGELOG.md').read_text('utf-8')
    require(f'## {version}\n' in changelog, 'CHANGELOG is missing the current version')
    if unreleased:
        tags = git('tag', '--list', 'v*').splitlines()
        prior = [tuple(map(int, name[1:].split('.'))) for name in tags
                 if SEMVER.fullmatch(name[1:])]
        require(not prior or tuple(map(int, version.split('.'))) > max(prior),
                'PR must select a new VERSION greater than all published version tags')
    audit = json.loads((ROOT / 'tools/protected_engine.json').read_text('utf-8'))
    protected = []
    sources = {}
    for record in audit['functions']:
        name, filename = record['name'], record['file']
        if filename not in sources:
            source = (ROOT / filename).read_text('utf-8')
            nodes = {node.name: node for node in ast.parse(source).body
                     if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
            sources[filename] = (source, nodes)
        source, nodes = sources[filename]
        require(name in nodes, f'Protected engine changed: missing {filename}:{name}')
        value = ast.get_source_segment(source, nodes[name])
        require(hashlib.sha256(value.encode()).hexdigest() == record['sha256'],
                f'Protected engine changed: {filename}:{name}')
        protected.append(filename + ':' + name)
    report = {'status': 'PASS', 'version': version, 'protected_functions': protected,
              'baseline_commit': audit['baseline_commit'],
              'scope': 'Release metadata and unchanged exact engine; real-market acceptance reported separately'}
    if tag is not None:
        require(tag == 'v' + version, 'Tag does not match VERSION')
        commit = git('rev-parse', 'HEAD')
        require(git('rev-parse', tag + '^{commit}') == commit, 'Tag does not point to checked-out commit')
        subprocess.run(['git', 'merge-base', '--is-ancestor', commit, 'origin/main'],
                       cwd=ROOT, check=True)
        report.update(tag=tag, commit=commit)
    if main_ci:
        require(tag is not None, '--main-ci requires --tag')
        repo = os.environ.get('GITHUB_REPOSITORY', '')
        require(repo == 'henrydoyle6/hlp-la-screen', 'Unexpected release repository')
        token = os.environ.get('GH_TOKEN', '')
        require(bool(token), 'GH_TOKEN required to read main CI status')
        query = urllib.parse.urlencode({'head_sha': commit, 'event': 'push',
                                        'branch': 'main', 'per_page': 100})
        request = urllib.request.Request(
            f'https://api.github.com/repos/{repo}/actions/workflows/ci.yml/runs?{query}',
            headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                     'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'hlp-release-gate'})
        with urllib.request.urlopen(request, timeout=30) as response:
            runs = json.load(response)['workflow_runs']
        runs = [r for r in runs if r['head_sha'] == commit and r['head_branch'] == 'main'
                and r['event'] == 'push']
        require(bool(runs), 'No main push CI run for this exact commit')
        latest = max(runs, key=lambda r: r['id'])
        require(latest['status'] == 'completed' and latest['conclusion'] == 'success',
                'Latest main CI for this exact commit has not succeeded')
        report.update(main_ci_run=latest['id'], main_ci_url=latest['html_url'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag')
    parser.add_argument('--main-ci', action='store_true')
    parser.add_argument('--unreleased', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = check(args.tag, args.main_ci, args.unreleased)
    except Exception as exc:
        parser.exit(1, f'Release check failed: {exc}\n')
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')


if __name__ == '__main__':
    main()
