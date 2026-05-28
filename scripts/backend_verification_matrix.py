from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = 'packages/agents:packages/data-core:packages/app'


def run_step(name: str, cmd: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
    )
    output = (result.stdout + '\n' + result.stderr).strip()
    ok = result.returncode == 0
    print(f'[{"OK" if ok else "FAIL"}] {name}')
    if output:
        print(output)
    print('---')
    return ok, output


def main() -> None:
    checks = [
        (
            'py_compile backend app/tests',
            [
                sys.executable,
                '-m',
                'py_compile',
                'packages/app/tests/test_failure_paths.py',
                'scripts/backend_real_data_smoke.py',
                'scripts/backend_api_contract_smoke.py',
                'scripts/live_runtime_smoke.py',
                'scripts/failure_path_smoke.py',
                'scripts/backend_verification_matrix.py',
            ],
            None,
        ),
        ('backend_api_contract_smoke', [sys.executable, 'scripts/backend_api_contract_smoke.py'], {'PYTHONPATH': PYTHONPATH}),
        ('live_runtime_smoke', [sys.executable, 'scripts/live_runtime_smoke.py'], {'PYTHONPATH': PYTHONPATH}),
        ('failure_path_smoke', [sys.executable, 'scripts/failure_path_smoke.py'], {'PYTHONPATH': PYTHONPATH}),
        ('backend_real_data_smoke', [sys.executable, 'scripts/backend_real_data_smoke.py'], {'PYTHONPATH': PYTHONPATH}),
        (
            'pytest failure/backend/live smoke',
            [
                sys.executable,
                '-X',
                'faulthandler',
                '-m',
                'pytest',
                'packages/app/tests/test_failure_paths.py',
                'packages/app/tests/test_backend_api_contract.py',
                'packages/app/tests/test_live_runtime_smoke.py',
                '-q',
            ],
            {'PYTHONPATH': PYTHONPATH},
        ),
    ]

    results: list[tuple[str, bool, str]] = []
    for name, cmd, env in checks:
        ok, output = run_step(name, cmd, env)
        results.append((name, ok, output))

    print('Verification matrix summary:')
    should_fail = False
    for name, ok, output in results:
        if name.startswith('pytest') and not ok and '_pytest/capture.py' in output:
            print(f'- {name}: ENV_ISSUE_SEGFAULT')
            continue
        if not ok:
            should_fail = True
            print(f'- {name}: FAIL')
        else:
            print(f'- {name}: PASS')

    if should_fail:
        sys.exit(1)


if __name__ == '__main__':
    main()
