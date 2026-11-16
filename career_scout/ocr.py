"""Local Windows OCR for public image job descriptions, with a content-addressed cache."""
import hashlib
import json
import subprocess
import sys

from .core import ROOT, data_dir


def read_image(content):
    if sys.platform != 'win32' or not content or len(content) > 8_000_000:
        raise ValueError('Local OCR unavailable or image size limit exceeded')
    ident = hashlib.sha256(content).hexdigest()
    folder = data_dir()/'ocr'
    folder.mkdir(exist_ok=True)
    result_path = folder/(ident+'.json')
    if result_path.exists():
        return json.loads(result_path.read_text(encoding='utf-8'))['text']
    image_path = folder/(ident+'.png')
    image_path.write_bytes(content)
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-File', str(ROOT/'scripts'/'local-ocr.ps1'), '-ImagePath', str(image_path)],
        capture_output=True, encoding='utf-8-sig', timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        check=True)
    data = json.loads(result.stdout)
    if not isinstance(data.get('text'), str) or not data['text'].strip():
        raise ValueError('Local OCR returned no text')
    result_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return data['text']
