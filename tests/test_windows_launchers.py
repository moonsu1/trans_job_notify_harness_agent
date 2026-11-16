"""Exercise actual CMD dispatch without registering tasks or starting paid work."""
import shutil
import subprocess
import sys

import pytest

from career_scout.core import ROOT


def test_cmd_launchers_use_windows_line_endings():
    for path in ROOT.glob('*.cmd'):
        raw = path.read_bytes()
        assert not raw.startswith(b'\xef\xbb\xbf'), path.name
        assert raw.count(b'\n') == raw.count(b'\r\n'), path.name


@pytest.mark.skipif(sys.platform != 'win32', reason='Requires Windows CMD')
@pytest.mark.parametrize(('filename', 'action'), [
    ('배치켜기.cmd', 'Enable'), ('배치끄기.cmd', 'Disable'), ('배치상태.cmd', 'Status'),
])
@pytest.mark.parametrize('exit_code', [0, 7])
def test_batch_controls_dispatch_and_preserve_exit_code(tmp_path, filename, action, exit_code):
    root = tmp_path/'한글 경로 with spaces'
    (root/'scripts').mkdir(parents=True)
    launcher = root/filename
    shutil.copyfile(ROOT/filename, launcher)
    # Stub only the target script. CMD and PowerShell really execute the launcher.
    (root/'scripts/manage-tasks.ps1').write_text(
        f'param([string]$Action)\nWrite-Output ("dispatched="+$Action)\nexit {exit_code}\n',
        encoding='ascii')
    result = subprocess.run(['cmd.exe','/d','/c','call',str(launcher)], cwd=tmp_path,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                            encoding='utf-8', errors='replace')
    assert 'dispatched='+action in result.stdout, result.stdout+result.stderr
    assert result.returncode == exit_code, result.stdout+result.stderr
