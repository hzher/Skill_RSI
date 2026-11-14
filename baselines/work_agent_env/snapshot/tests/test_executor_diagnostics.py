"""Failures remain attributable without leaking keys; retry only transient requests."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from integrations.executors import ExecutorOptions
from integrations.executors.diagnostics import error_details
from integrations.executors.models import APIModel


class Failure(Exception):
    def __init__(self,status,message):
        super().__init__(message)
        self.status_code=status
        self.body={'error':{'message':message,'type':'invalid_request_error','param':'messages'}}


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def model(self):
        model=object.__new__(APIModel)
        model.options=ExecutorOptions('openai',model='fixed')
        return model

    async def test_400_preserves_sanitized_body_and_identical_request_fingerprint_without_retry(self):
        create=AsyncMock(side_effect=Failure(400,'invalid messages key test-only-api-secret'))
        with patch.dict(os.environ,{'MODEL_API_KEY':'test-only-api-secret'}):
            with self.assertRaises(Failure) as caught:
                await self.model().request(create,model='fixed',messages=[{'role':'user','content':'task'}],max_completion_tokens=128)
        error=caught.exception
        self.assertEqual(create.await_count,1)
        self.assertNotIn('test-only-api-secret',str(error.request_diagnostics))
        self.assertIn('invalid messages',error.request_diagnostics['attempts'][0]['provider_error']['message'])
        self.assertEqual(error.request_diagnostics['request']['output_budget'],128)

    async def test_503_retries_the_same_request_and_keeps_all_attempts(self):
        create=AsyncMock(side_effect=[Failure(503,'upstream unavailable'),SimpleNamespace(ok=True)])
        model=self.model()
        with patch('integrations.executors.models.asyncio.sleep',new=AsyncMock()):
            result=await model.request(create,model='fixed',messages=[])
        self.assertTrue(result.ok)
        self.assertEqual(create.await_count,2)
        self.assertEqual(create.await_args_list[0],create.await_args_list[1])
        self.assertEqual(len(model.last_request['attempts']),2)

    async def test_generic_upstream_400_is_not_assumed_transient(self):
        create=AsyncMock(side_effect=Failure(400,'Upstream request failed（上游请求失败，请稍后重试）'))
        model=self.model()
        with patch('integrations.executors.models.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(Failure):await model.request(create,model='fixed',messages=[])
        self.assertEqual(create.await_count,1)
