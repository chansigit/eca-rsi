"""#52 (#31 step 1): every model turn records its size, images, latency and the class of its failure."""
import asyncio
import base64
import json

import httpx

from ecarsi.agent.dispatch import call_totals, error_class
from ecarsi.agent.session import record_calls, request_size
from ecarsi.files import read

PNG = base64.b64encode(b"\x89PNG" + b"x" * 2996).decode()  # 3,000 bytes


def test_a_request_is_measured_by_its_bytes_and_inlined_images():
    body = json.dumps({"input": [{"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"},
                                 {"type": "input_text", "text": "two figures"},
                                 {"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}]}).encode()
    assert request_size(body) == dict(request_bytes=len(body), images=2, image_bytes=6000)
    assert request_size(b'{"input": "text only"}')["images"] == 0


def test_the_provider_client_records_each_call(tmp_path):
    def provider(request):
        if b"overloaded" in request.content:
            return httpx.Response(429, json={"error": {"code": "ServerOverloaded", "message": "retry later"}})
        return httpx.Response(200, json={"id": "r1", "status": "completed",
                                         "usage": {"input_tokens": 1200, "output_tokens": 40}, "output": []})

    async def two_calls():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
            record_calls(http, tmp_path)
            await http.post("https://provider.test/v1/responses", json={"input": "overloaded"})
            await http.post("https://provider.test/v1/responses",
                            json={"input": [{"image_url": f"data:image/png;base64,{PNG}"}]})
    asyncio.run(two_calls())
    first, second = read(tmp_path / "provider-calls.json")
    assert first["http_status"] == 429 and first["error_code"] == "ServerOverloaded" and first["input_tokens"] is None
    assert second["http_status"] == 200 and (second["images"], second["image_bytes"]) == (1, 3000)
    assert (second["input_tokens"], second["output_tokens"]) == (1200, 40) and second["latency_s"] >= 0
    assert read(tmp_path / "provider-response.json")["status"] == "completed"
    assert call_totals([first, second]) == dict(input_tokens=1200, output_tokens=40, images=1, image_bytes=3000,
                                                latency_s=first["latency_s"] + second["latency_s"])
    assert call_totals([]) == dict(input_tokens=None, output_tokens=None, images=None, image_bytes=None, latency_s=None)


def test_failures_are_classified_from_what_result_json_keeps():
    """The error texts are the provider's and the SDK's own, from batch-1 turns of 2026-10-06."""
    length = ("Responses stream ended with terminal event `response.incomplete`. status=incomplete; "
              "incomplete_details=IncompleteDetails(reason='length').")
    cases = [
        (("success", None, None, None), None),
        (("timeout", "TimeoutError", "", None), "timeout"),
        (("provider_error", "APITimeoutError", "Request timed out.", None), "timeout"),
        (("provider_error", "RateLimitError", "Error code: 429 - {'error': {'code': 'ServerOverloaded'}}",
          {"http_status": 429, "error_code": "ServerOverloaded"}), "rate_limit"),
        (("provider_error", "BadRequestError", "Total tokens of image and text exceed max message tokens", None),
         "context_too_long"),
        (("provider_error", "BadRequestError", "Maximum of 1000 items allowed in input.", None), "context_too_long"),
        (("provider_error", "ModelBehaviorError", length, {"incomplete_details": {"reason": "length"}}), "output_limit"),
        (("incomplete_submission", "Required completion tool was not called", None,
          {"incomplete_details": {"reason": "max_output_tokens"}}), "output_limit"),
        (("provider_error", "JSONDecodeError", "Expecting property name enclosed in double quotes: line 1", None),
         "parse_error"),
        (("provider_error", "APIConnectionError", "Connection error.", None), "other"),
        (("provider_error", "InternalServerError", "Error code: 500", {"http_status": 500}), "other"),
        (("worker_setup_timeout", "TimeoutExpired", "bashrc", None), "other"),
        (("worker_lost", None, None, None), "other"),
    ]
    assert [error_class(*args) for args, _ in cases] == [expected for _, expected in cases]
