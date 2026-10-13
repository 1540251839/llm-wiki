import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import patch

from researchwiki.llm.client import LLMClient, StructuredOutputError


def completion(content, finish_reason="stop", usage=None):
    return io.BytesIO(json.dumps({
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }).encode("utf-8"))


class LLMJSONReliabilityTest(unittest.TestCase):
    def client(self, **values):
        return LLMClient({"provider": "deepseek", "model": "fixture-model",
                          "api_key": "test-only-secret-key", **values})

    def test_request_explicit_token_limit_and_json_mode(self):
        client = self.client()
        with patch("urllib.request.urlopen", return_value=completion('{"title":"Source"}')) as request:
            self.assertEqual(client.structured("Source-only facts", "Read this."), {"title": "Source"})
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("JSON", body["messages"][0]["content"])
        self.assertEqual(client.last_response_meta["request_attempts"], 1)
        self.assertEqual(client.last_response_meta["json_attempts"], 1)
        self.assertEqual(client.last_response_meta["finish_reason"], "stop")
        self.assertEqual(client.last_response_meta["usage"]["completion_tokens"], 20)
        self.assertNotIn(client.api_key, json.dumps(client.last_response_meta))

    def test_token_limit_override_does_not_mutate_config(self):
        client = self.client(max_output_tokens=4000)
        with patch("urllib.request.urlopen", return_value=completion('{"ok":true}')) as request:
            client.structured("JSON", "Source", max_output_tokens=9000)
        self.assertEqual(json.loads(request.call_args.args[0].data)["max_tokens"], 9000)
        self.assertEqual(client.max_output_tokens, 4000)

    def test_complete_outer_code_fence_is_allowed(self):
        client = self.client()
        with patch.object(client, "generate", return_value='  ```json\n{"title": "A"}\n```  '):
            self.assertEqual(client.structured("s", "u"), {"title": "A"})

    def test_extra_prose_partial_prefix_and_trailing_objects_are_rejected(self):
        for text in ['Explanation: {"ok":true}', '{"ok":true}\nExtra text',
                     '{"ok":true}{"another":true}',
                     '```json\n{"ok":true}\n```\nExplanation',
                     '{"claims":[{"statement":"first"}]},"unfinished":']:
            with self.subTest(text=text):
                client = self.client(json_max_retries=0)
                with patch.object(client, "generate", return_value=text):
                    with self.assertRaises(StructuredOutputError) as caught:
                        client.structured("s", "u")
                self.assertEqual(caught.exception.reason, "invalid_json")

    def test_valid_object_marked_length_is_never_accepted_or_retried(self):
        client = self.client(json_max_retries=3)
        with patch("urllib.request.urlopen", return_value=completion('{"ok":true}', "length")) as request:
            with self.assertRaises(StructuredOutputError) as caught:
                client.structured("s", "u")
        self.assertEqual(caught.exception.reason, "truncated")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(caught.exception.metadata["finish_reason"], "length")
        self.assertEqual(caught.exception.metadata["request_attempts"], 1)

    def test_non_json_analysis_detects_truncation_too(self):
        client = self.client()
        with patch("urllib.request.urlopen", return_value=completion("unfinished note", "length")):
            with self.assertRaises(StructuredOutputError) as caught:
                client.generate("s", "u")
        self.assertEqual(caught.exception.reason, "truncated")

    def test_generate_override_can_report_truncation_via_metadata(self):
        client = self.client()

        def generate(_system, _user, temperature=0, json_mode=True):
            client.last_response_meta = {"finish_reason": "length", "request_attempts": 1}
            return '{"claims": []}'

        with patch.object(client, "generate", side_effect=generate) as request:
            with self.assertRaises(StructuredOutputError) as caught:
                client.structured("s", "u")
        self.assertEqual(caught.exception.reason, "truncated")
        self.assertEqual(request.call_count, 1)

    def test_json_retry_regenerates_from_source_not_failed_output(self):
        client = self.client(json_max_retries=1)
        broken = '{"invented_fact_from_bad_output": 1 "title": "X"}'
        with patch.object(client, "generate", side_effect=[broken, '{"title":"Source"}']) as generate:
            self.assertEqual(client.structured("s", "Original source only"), {"title": "Source"})
        retry_user = generate.call_args_list[1].args[1]
        self.assertTrue(retry_user.startswith("Original source only"))
        self.assertIn("Regenerate the entire JSON object", retry_user)
        self.assertIn("do not invent missing facts", retry_user)
        self.assertNotIn("invented_fact_from_bad_output", retry_user)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(client.last_response_meta["json_attempts"], 2)
        self.assertEqual(client.last_response_meta["request_attempts"], 2)

    def test_repeated_malformed_response_exhausts_bounded_format_budget(self):
        client = self.client(json_max_retries=1)
        with patch.object(client, "generate", return_value='{"not closed":') as generate:
            with self.assertRaises(StructuredOutputError) as caught:
                client.structured("s", "u")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(caught.exception.reason, "invalid_json")
        self.assertEqual(caught.exception.metadata["json_attempts"], 2)
        self.assertEqual(caught.exception.metadata["json_retries"], 1)

    def test_empty_content_gets_only_the_format_retry_budget(self):
        client = self.client(json_max_retries=1)
        with patch("urllib.request.urlopen", side_effect=[completion(""), completion('{"ok":true}')]) as request:
            self.assertEqual(client.structured("s", "u"), {"ok": True})
        self.assertEqual(request.call_count, 2)
        self.assertEqual(client.last_response_meta["json_attempts"], 2)

    def test_not_an_object_and_non_finite_json_numbers_are_rejected(self):
        for text, reason in [('[]', "invalid_object"), ('"scalar"', "invalid_object"),
                             ('{"metric": NaN}', "invalid_json"), ('{"metric": Infinity}', "invalid_json")]:
            with self.subTest(text=text):
                client = self.client(json_max_retries=0)
                with patch.object(client, "generate", return_value=text):
                    with self.assertRaises(StructuredOutputError) as caught:
                        client.structured("s", "u")
                self.assertEqual(caught.exception.reason, reason)

    def test_usage_and_request_counts_aggregate_across_json_and_http_retries(self):
        client = self.client(json_max_retries=1, max_retries=1)
        http_error = urllib.error.HTTPError("https://example.invalid", 503, "Unavailable", {}, io.BytesIO(b""))
        with patch("urllib.request.urlopen", side_effect=[http_error, completion('{"broken":'), completion('{"ok":true}')]) as request, \
             patch("time.sleep"):
            self.assertEqual(client.structured("s", "u"), {"ok": True})
        self.assertEqual(request.call_count, 3)
        self.assertEqual(client.last_response_meta["requests"], 3)
        self.assertEqual(client.last_response_meta["attempts"], 3)
        self.assertEqual(client.last_response_meta["json_attempts"], 2)
        self.assertEqual(client.last_response_meta["usage_totals"]["completion_tokens"], 40)
        self.assertEqual(client.last_response_meta["usage_totals"]["total_tokens"], 240)
        self.assertIn("total_elapsed_ms", client.last_response_meta)

    def test_network_budget_does_not_restart_for_json_retry(self):
        client = self.client(max_retries=1, json_max_retries=3)
        with patch("urllib.request.urlopen", side_effect=socket.timeout("test-only-secret-key")) as request:
            with self.assertRaises(StructuredOutputError) as caught:
                client.structured("s", "u")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(caught.exception.reason, "request_failed")
        self.assertEqual(caught.exception.metadata["requests"], 2)
        self.assertEqual(caught.exception.metadata["json_attempts"], 1)
        self.assertNotIn(client.api_key, str(caught.exception))
        self.assertNotIn(client.api_key, json.dumps(caught.exception.metadata))

    def test_non_retryable_http_error_does_not_expose_provider_body(self):
        client = self.client(max_retries=5)
        provider_body = b'{"message":"bad key test-only-secret-key"}'
        exc = urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(provider_body))
        with patch("urllib.request.urlopen", side_effect=exc) as request:
            with self.assertRaises(StructuredOutputError) as caught:
                client.structured("s", "u")
        self.assertEqual(request.call_count, 1)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn(client.api_key, str(caught.exception))

    def test_content_filter_is_not_treated_as_valid_partial_json(self):
        client = self.client()
        with patch("urllib.request.urlopen", return_value=completion('{"ok": true}', "content_filter")):
            with self.assertRaises(StructuredOutputError) as caught:
                client.structured("s", "u")
        self.assertEqual(caught.exception.reason, "incomplete_response")
        self.assertFalse(caught.exception.retryable)

    def test_stale_metadata_does_not_make_a_new_response_truncated(self):
        client = self.client()
        client.last_response_meta = {"finish_reason": "length", "usage": {"completion_tokens": 8192}}
        with patch.object(client, "generate", return_value='{"ok": true}'):
            self.assertEqual(client.structured("s", "u"), {"ok": True})
        self.assertIsNone(client.last_response_meta["finish_reason"])

    def test_stream_is_not_retried_after_a_partial_answer(self):
        client = self.client(max_retries=3)
        partial = io.BytesIO(b'data: {"choices": [{"delta": {"content": "first"}}]}\n\n')
        with patch("urllib.request.urlopen", return_value=partial) as request:
            stream = client.stream_generate("s", "u")
            self.assertEqual(next(stream), "first")
            with self.assertRaisesRegex(RuntimeError, "before completion"):
                next(stream)
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
