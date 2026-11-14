import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coach import config, omlx, ui


class ProviderTests(unittest.TestCase):
    def config(self, text, env=None):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,env or {},clear=True):
            path=Path(folder)/'.env';path.write_text(text)
            return config.load(path)

    def test_legacy_omlx_settings_and_secret_repr(self):
        cfg=self.config('OMLX_BASE_URL=http://localhost:8000/v1\nOMLX_MODEL=local-test\nOMLX_API_KEY=private-test-key\nAGX_API_KEY=private-agx-key\n')
        self.assertEqual(cfg.llm_provider,'omlx');self.assertEqual(cfg.omlx_model,'local-test')
        self.assertTrue(cfg.llm_fallback_models)
        self.assertNotIn('private-test-key',repr(cfg));self.assertNotIn('private-agx-key',repr(cfg))

    def test_env_and_generic_settings_override_legacy(self):
        cfg=self.config('LLM_PROVIDER=omlx\nLLM_MODEL=file-model\nOMLX_MODEL=legacy\n',{'LLM_MODEL':'environment-model'})
        self.assertEqual(cfg.omlx_model,'environment-model')

    def test_openai_payload_has_no_local_or_unsupported_sampling_fields(self):
        cfg=self.config('LLM_PROVIDER=openai\nLLM_MODEL=reasoning-test\nLLM_API_KEY=private-test-key\nOMLX_MODEL=must-not-leak\n')
        body=omlx.request_payload(cfg,cfg.omlx_model,[{'role':'user','content':'test'}],123,
                                  {'temperature':.7,'top_p':.9,'top_k':40},True,stream=True)
        self.assertEqual(body['max_completion_tokens'],123)
        self.assertTrue(body['stream']);self.assertEqual(cfg.llm_fallback_models,())
        for key in ('max_tokens','chat_template_kwargs','top_k','top_p','temperature'):self.assertNotIn(key,body)

    def test_compatible_payload_uses_standard_fields_and_auth(self):
        cfg=self.config('LLM_PROVIDER=openai_compatible\nLLM_BASE_URL=https://example.invalid/v1\nLLM_MODEL=hosted-test\nLLM_API_KEY=private-test-key\n')
        body=omlx.request_payload(cfg,cfg.omlx_model,[],88,{'temperature':.5,'top_p':.8,'top_k':50},True)
        self.assertEqual(body['max_tokens'],88);self.assertEqual(body['temperature'],.5)
        self.assertNotIn('top_k',body);self.assertNotIn('chat_template_kwargs',body)
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        response=Response(json.dumps({'choices':[{'message':{'content':'{"ok":true}'}}]}).encode())
        with patch.object(omlx,'load',return_value=cfg),patch.object(omlx,'_trace'),patch.object(omlx.urllib.request,'urlopen',return_value=response) as opened:
            result=omlx.chat_json(cfg.omlx_url,cfg.omlx_model,[],cfg.omlx_key)
        self.assertTrue(result['ok'])
        request=opened.call_args.args[0]
        self.assertEqual(request.full_url,'https://example.invalid/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'),'Bearer private-test-key')

    def test_hosted_provider_does_not_try_local_models(self):
        cfg=self.config('LLM_PROVIDER=openrouter\nLLM_MODEL=test/provider-model\nLLM_API_KEY=private-test-key\n')
        with patch.object(omlx,'load',return_value=cfg),patch.object(omlx,'_complete',side_effect=omlx.OmlxError('failed to load')) as complete:
            with self.assertRaises(omlx.OmlxError):omlx.chat_text(cfg.omlx_url,cfg.omlx_model,[],cfg.omlx_key)
        self.assertEqual(complete.call_count,1)

    def test_missing_hosted_settings_are_actionable(self):
        for value in ('LLM_PROVIDER=unknown\n','LLM_PROVIDER=openai\n','LLM_PROVIDER=openai\nLLM_MODEL=test\n'):
            with self.assertRaises(ValueError):self.config(value)

    def test_dashboard_model_discovery_authenticates_without_exposing_key(self):
        cfg=self.config('LLM_PROVIDER=openai\nLLM_MODEL=test\nLLM_API_KEY=private-test-key\n')
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        with patch.object(ui,'load',return_value=cfg),patch.object(ui.urllib.request,'urlopen',return_value=Response(b'{"data":[{"id":"test"}]}')) as opened:
            result=ui._omlx_models()
        self.assertEqual(opened.call_args.args[0].get_header('Authorization'),'Bearer private-test-key')
        self.assertNotIn('private-test-key',json.dumps(result))

    def test_local_payload_retains_extensions(self):
        cfg=self.config('')
        body=omlx.request_payload(cfg,'local',[],32,{'top_k':20},True)
        self.assertEqual(body['top_k'],20);self.assertEqual(body['chat_template_kwargs'],{'enable_thinking':True})


if __name__=='__main__':unittest.main()
