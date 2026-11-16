"""Public response evidence only. No headers, cookies, API keys or profiles."""
import contextvars
import json
from pathlib import Path

from bs4 import BeautifulSoup

from .core import data_dir, digest, now

SOURCE = contextvars.ContextVar('career_scout_source', default='')


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else ['empty']
    return type(value).__name__


def schema_hash(text):
    try:
        return digest(shape(json.loads(text)))
    except (ValueError, TypeError):
        soup = BeautifulSoup(text[:200000], 'html.parser')
        return digest(sorted({(tag.name, ' '.join(tag.get('class', [])))
                              for tag in soup.find_all(limit=3000)}))


class EvidenceVault:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_dir()/'maintenance'
        (self.root/'evidence').mkdir(parents=True, exist_ok=True)
        self.records = {}

    def capture(self, url, text, *, status=200, company_id='', kind='http', request=None):
        # Full evidence for validation is bounded separately from model inputs.
        text = text[:2_000_000]
        company_id = company_id or SOURCE.get()
        ident = digest([url, status, text, company_id, request])[:32]
        record = dict(id=ident, url=url, status=status, text=text, company_id=company_id or SOURCE.get(),
                      at=now().isoformat(), schema_hash=schema_hash(text), kind=kind, request=request)
        self.records[ident] = record
        (self.root/'evidence'/(ident+'.json')).write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        return record

    def load(self, ident):
        if len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
            raise ValueError('Invalid evidence ID')
        return json.loads((self.root/'evidence'/(ident+'.json')).read_text(encoding='utf-8'))

    def for_company(self, company_id):
        return [r for r in self.records.values() if r['company_id'] == company_id]
