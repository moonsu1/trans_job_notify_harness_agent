from types import SimpleNamespace as NS

import httpx
import pytest
from openai import BadRequestError, OpenAI

from career_scout.agent import Agent, AgentError
from career_scout.errors import error_detail, known_unbilled_failure


def test_openai_sdk_resource_import_works_on_this_windows():
    # Resources import jiter lazily. A plain `import openai` missed the production DLL block.
    client = OpenAI(api_key='not-a-real-key')
    assert client.responses is not None
    client.close()


def test_errors_redact_credentials():
    secret = 'sk-proj-do-not-display-this-secret'
    detail = error_detail(ValueError('bad key '+secret+' and mail-password'), (secret, 'mail-password'))
    assert secret not in detail and 'mail-password' not in detail
    assert '[REDACTED]' in detail


@pytest.mark.parametrize('error', [ImportError('DLL blocked'), UnicodeEncodeError('ascii', '\ud55c', 0, 1, 'bad')])
def test_local_errors_release_budget(config, store, job, error):
    class Responses:
        def create(self, **kwargs):
            raise error
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    agent.client = NS(responses=Responses())
    with pytest.raises(AgentError):
        agent.analyze(job)
    assert store.db.execute('SELECT actual FROM usage').fetchone()[0] == 0
    assert store.db.execute('SELECT status FROM usage').fetchone()[0] == 'rejected'


def test_http_rejection_is_unbilled():
    request = httpx.Request('POST', 'https://api.openai.com/v1/responses')
    exc = BadRequestError('unsupported parameter', response=httpx.Response(400, request=request),
                          body={'error': {'code': 'unsupported_parameter', 'param': 'reasoning'}})
    assert known_unbilled_failure(exc)
    assert 'unsupported_parameter' in error_detail(exc)
    assert 'HTTP 400' in error_detail(exc)
    assert not known_unbilled_failure(ConnectionError('network interrupted'))
