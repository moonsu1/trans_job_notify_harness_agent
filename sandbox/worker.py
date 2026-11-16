"""Trusted container entry point; no network, credentials or host write mounts."""
import importlib.util
import json
import sys


class API:
    def call(self, name, **args):
        print(json.dumps({'op': 'tool', 'name': name, 'args': args}), flush=True)
        result = json.loads(sys.stdin.readline())
        if not isinstance(result, dict) or 'id' not in result:
            raise ValueError('Tool result unavailable')
        return result

    def fetch(self, url, **kwargs):
        return self.call('fetch', url=url, **kwargs)

    def render(self, url, **kwargs):
        return self.call('render', url=url, **kwargs)

    def jobflex(self, path, payload=None):
        return self.call('jobflex', path=path, payload=payload)


data = json.loads(sys.stdin.readline())
spec = importlib.util.spec_from_file_location('candidate', '/plugin/plugin.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = module.collect(API(), data['company'])
print(json.dumps({'op': 'result', 'result': result}, ensure_ascii=False), flush=True)
