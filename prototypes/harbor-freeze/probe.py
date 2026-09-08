"""THROWAWAY: actual Docker/Harbor lifecycle probe, no model calls."""
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
IMAGE = 'python@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254'
HARBOR_COMMIT = '71c39eafbd134d43ae3f489b5e6488b2a157de65'


def command(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, timeout=20, check=check)


def hashes(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.iterdir()) if p.is_file()}


def save(path, value):
    # Process-restart experiment, not a claim of power-loss durability.
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2))
    temp.replace(path)


def collect(cid, directory, reason, fault='none', finish='kill'):
    directory.mkdir(parents=True, exist_ok=True)
    # ponytail: one local filesystem lock per answer; distributed storage needs another design.
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt_path = directory / 'receipt.json'
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            assert hashes(directory / 'answer') == receipt['hashes']
            return receipt
        state_path = directory / 'state.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
        else:
            requested = time.time()
            command('docker', 'pause', cid)
            state = {'cid': cid, 'reason': reason, 'requested_at': requested,
                     'frozen_at': time.time(), 'status': 'frozen'}
            save(state_path, state)
        actual = json.loads(command('docker', 'inspect', cid).stdout)[0]['State']
        assert state['cid'] == cid and actual['Paused'], 'Cannot prove frozen state'
        if fault == 'crash':
            os._exit(77)  # Actual collector process dies; container stays paused.
        pending = directory / 'pending'
        if pending.exists():
            shutil.rmtree(pending)
        pending.mkdir()
        source = '/missing' if fault == 'copy' else '/workspace/.'
        result = command('docker', 'cp', f'{cid}:{source}', str(pending), check=False)
        if result.returncode:
            state.update(status='collection_failed', error=result.stderr.strip())
            save(state_path, state)
            raise RuntimeError(result.stderr.strip())
        before = hashes(pending)
        time.sleep(0.2)
        audit = directory / 'audit'
        audit.mkdir(exist_ok=True)
        command('docker', 'cp', f'{cid}:/workspace/.', str(audit))
        assert before == hashes(audit), 'Answer changed while paused'
        assert {'parent.txt', 'child.txt', 'added.bin'} <= before.keys()
        shutil.rmtree(audit)
        # ponytail: crash after this rename but before receipt publication is not recovered;
        # production needs a staged, recoverable publication protocol.
        pending.rename(directory / 'answer')
        if finish == 'kill':
            command('docker', 'kill', cid)
            assert not json.loads(command('docker', 'inspect', cid).stdout)[0]['State']['Running']
        receipt = {**state, 'status': 'collected', 'collected_at': time.time(),
                   'hashes': before, 'finish': finish}
        save(receipt_path, receipt)
        return receipt


# The custom agent is only the controlled stimulus, not a new production harness.
from harbor.agents.nop import NopAgent
from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.models.trial.config import TrialConfig, TaskConfig, AgentConfig, VerifierConfig, EnvironmentConfig
from harbor.trial.trial import Trial
from harbor.trial.hooks import TrialEvent


class WriterAgent(NopAgent):
    @staticmethod
    def name():
        return 'aco-prototype-writer'

    async def setup(self, environment):
        await environment.upload_file(HERE / 'writer.py', '/tmp/writer.py')

    async def run(self, instruction, environment, context):
        result = await environment.exec('python /tmp/writer.py ' + shlex.quote(instruction.strip()))
        if result.return_code:
            raise NonZeroAgentExitCodeError(f'Writer exit {result.return_code}')


async def run_case(root, mode):
    name = 'aco-freeze-' + mode.replace('_', '-') + '-' + uuid.uuid4().hex[:8]
    task_dir = root / name / 'task'
    (task_dir / 'environment').mkdir(parents=True)
    (task_dir / 'instruction.md').write_text(mode)
    (task_dir / 'task.toml').write_text(
        f'schema_version = "1.4"\n[environment]\ndocker_image = "{IMAGE}"\n'
        f'network_mode = "public"\n[agent]\ntimeout_sec = {2.5 if mode == "timeout" else 15}\n')
    (task_dir / 'environment' / 'Dockerfile').write_text(f'FROM {IMAGE}\n')
    # Test Docker's native no-network mode; Harbor's allowlist sidecar is outside this probe.
    compose = task_dir / 'offline.yaml'
    compose.write_text(f'services:\n  main:\n    network_mode: none\n    labels:\n      aco.prototype.run: {name}\n')
    trial = await Trial.create(TrialConfig(
        task=TaskConfig(path=task_dir), trial_name=name, trials_dir=root / 'trials',
        agent=AgentConfig(import_path='probe:WriterAgent'),
        environment=EnvironmentConfig(extra_docker_compose=[compose]),
        verifier=VerifierConfig(disable=True), artifacts=['/workspace']))
    directory = root / name / 'official'
    events = []
    cid = None

    async def collector(reason, fault='none', finish='kill'):
        result = await asyncio.to_thread(command, sys.executable, str(HERE / 'probe.py'),
            'collect', cid, str(directory), reason, fault, finish, check=False)
        events.append({'action': 'collect', 'reason': reason, 'fault': fault,
                       'exit_code': result.returncode, 'at': time.time()})
        return result

    async def begin(event):
        nonlocal cid
        ids = command('docker', 'ps', '-q', '--filter', f'label=aco.prototype.run={name}',
                      '--filter', 'label=com.docker.compose.service=main').stdout.split()
        assert len(ids) == 1
        cid = ids[0]
        events.append({'action': 'agent_start', 'at': time.time(), 'cid': cid})

    async def end(event):
        events.append({'action': 'agent_end_hook', 'at': time.time()})
        if mode == 'control':
            samples = []
            for i in range(2):
                sample = root / name / f'sample-{i}'
                sample.mkdir()
                await asyncio.to_thread(command, 'docker', 'cp', f'{cid}:/workspace/.', str(sample))
                samples.append(hashes(sample))
                await asyncio.sleep(0.3)
            assert samples[0]['child.txt'] != samples[1]['child.txt']
            events.append({'action': 'background_writer_survived_agent_exit', 'samples': samples})
        elif mode not in ('submit', 'crash', 'copy_failure'):
            result = await collector('timeout' if mode == 'timeout' else 'agent_exit', finish='pause')
            assert result.returncode == 0, result.stderr

    trial.add_hook(TrialEvent.AGENT_START, begin)
    trial.add_hook(TrialEvent.AGENT_END, end)

    async def watch_submit():
        marker = trial.paths.artifacts_dir / 'logs' / 'artifacts' / 'submit.request'
        while not marker.exists():
            await asyncio.sleep(0.03)
        fault = {'crash': 'crash', 'copy_failure': 'copy'}.get(mode, 'none')
        if fault != 'none':
            failed = await collector('submit', fault)
            assert failed.returncode != 0 and not (directory / 'receipt.json').exists()
            assert (directory / 'state.json').exists()
        # Duplicate requests compete via a real OS file lock in distinct processes.
        results = await asyncio.gather(collector('submit'), collector('submit'))
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]

    watcher = asyncio.create_task(watch_submit()) if mode in ('submit', 'crash', 'copy_failure') else None
    try:
        result = await asyncio.wait_for(trial.run(), timeout=45)
        if watcher:
            await asyncio.wait_for(watcher, timeout=15)
        exception = result.exception_info.exception_type if result.exception_info else None
        if mode != 'control':
            receipt = json.loads((directory / 'receipt.json').read_text())
            again = await collector('duplicate_after_trial_cleanup')
            assert again.returncode == 0, again.stderr
            assert json.loads((directory / 'receipt.json').read_text()) == receipt
            # Independent read-only verifier process, no Harbor score or live workspace.
            check = command('docker', 'run', '--rm', '--network', 'none',
                '--mount', f'type=bind,source={directory / "answer"},target=/answer,readonly',
                IMAGE, 'python', '-c',
                'from pathlib import Path; p=Path("/answer"); '
                'assert int((p/"parent.txt").read_text())>=0; '
                'assert int((p/"child.txt").read_text())>=0; '
                'assert (p/"added.bin").read_bytes()==bytes(range(256)); print("PASS")')
            assert check.stdout.strip() == 'PASS'
            assert hashes(directory / 'answer') == receipt['hashes']
        else:
            receipt = None
        harbor_answer = hashes(trial.paths.artifacts_dir / 'workspace')
        outcome = {'harbor_artifact_hashes': harbor_answer,
                   'harbor_artifacts_match_official': harbor_answer == receipt['hashes'] if receipt else None,
                   'mode': mode, 'passed_checks': True, 'harbor_exception': exception,
                   'harbor_scored': result.verifier_result is not None,
                   'receipt': receipt, 'events': events}
        assert outcome['harbor_scored'] is False
        save(root / name / 'observations.json', outcome)
        print(json.dumps(outcome), flush=True)
        return outcome
    finally:
        if watcher and not watcher.done():
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if cid:
            await asyncio.to_thread(command, 'docker', 'rm', '-f', cid, check=False)


async def main():
    root = HERE / ('PROTOTYPE-results-' + uuid.uuid4().hex[:8])
    root.mkdir()
    results = []
    for mode in sys.argv[1:] or ('control', 'exit', 'timeout', 'submit', 'crash', 'copy_failure'):
        results.append(await run_case(root, mode))
    save(root / 'summary.json', {'harbor_source': HARBOR_COMMIT,
        'python': sys.version, 'image': IMAGE, 'cases': results})
    print('RESULTS:', root)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'collect':
        print(json.dumps(collect(sys.argv[2], Path(sys.argv[3]), *sys.argv[4:])))
    else:
        asyncio.run(main())
