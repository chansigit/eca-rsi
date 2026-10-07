"""#54: a catalog entry with its own key variable and endpoint, keys from a private keys file."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading

import pytest

import ecarsi.agent as bridge
import ecarsi.agent.session as session
from ecarsi.agent import dispatch
from ecarsi.agent.dispatch import model_key
from ecarsi.agent.runner import serve_runner
from ecarsi.files import read, reference, save
from ecarsi.model_web import normalized_models

PLAN = "https://ark.cn-beijing.volces.com/api/plan/v3"


def test_entries_may_name_their_own_key_and_endpoint():
    old = dict(harness="openai", model="doubao-seed-2-1-turbo-260628", url="https://ark.cn-beijing.volces.com/api/v3")
    plan = dict(harness="openai", model="glm-5-3-flash", url=PLAN, key_env="ARK_PLAN_API_KEY")
    models = normalized_models(dict(models=[old, plan]))
    assert models[0] == old and "key_env" not in models[0]  # old entries keep their identity (runner and health keys)
    assert models[1]["key_env"] == "ARK_PLAN_API_KEY" and models[1]["url"] == PLAN
    with pytest.raises(ValueError, match="share its API base URL"):  # without key_env, one URL per backend as before
        normalized_models(dict(models=[old, dict(old, model="pro", url=PLAN)]))
    with pytest.raises(ValueError, match="key variable needs its own API base URL"):
        normalized_models(dict(models=[dict(plan, url="")]))
    with pytest.raises(ValueError, match="names an environment variable"):
        normalized_models(dict(models=[dict(plan, key_env="ark-7f7e")]))  # a key pasted where its name belongs
    with pytest.raises(ValueError, match="Only backend"):
        normalized_models(dict(models=[dict(plan, api_key="x")]))


def test_the_worker_reads_its_key_from_the_private_keys_file(tmp_path, monkeypatch):
    keys = tmp_path / "keys.env"
    keys.write_text("# comment\nARK_API_KEY_2=fake-second\nexport ARK_PLAN_API_KEY='fake-plan'\nEMPTY=\n")
    keys.chmod(0o600)
    monkeypatch.setenv("ECA_KEYS_FILE", str(keys))
    monkeypatch.delenv("ARK_PLAN_API_KEY", raising=False)
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *a, **k: pytest.fail("the shell is not consulted"))
    assert dispatch.read_keys_file() == {"ARK_API_KEY_2": "fake-second", "ARK_PLAN_API_KEY": "fake-plan"}
    dispatch.load_worker_key(dict(harness="openai", model="m", url=PLAN, key_env="ARK_PLAN_API_KEY"))
    assert dispatch.os.environ["ARK_PLAN_API_KEY"] == "fake-plan"
    monkeypatch.setenv("ARK_API_KEY", "from-environment")  # the environment wins over the file
    dispatch.load_worker_key(dict(harness="openai", model="m", url=""))
    assert dispatch.os.environ["ARK_API_KEY"] == "from-environment"
    keys.chmod(0o644)
    with pytest.raises(ValueError, match="must be private"):
        dispatch.read_keys_file()


class Provider(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        Provider.seen.append(self.headers["Authorization"])
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = json.dumps(dict(id="r", object="chat.completion", created=1, model=body["model"],
            choices=[dict(index=0, finish_reason="stop", message=dict(role="assistant", content="Done"))],
            usage=dict(prompt_tokens=3, completion_tokens=1, total_tokens=4))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


def test_a_turn_on_an_entry_with_its_own_key_sends_that_key(tmp_path, monkeypatch):
    keys = tmp_path / "keys.env"
    keys.write_text("ARK_PLAN_API_KEY=fake-plan\n")
    keys.chmod(0o600)
    monkeypatch.setenv("ECA_KEYS_FILE", str(keys))
    monkeypatch.setenv("ARK_API_KEY", "fake-production")
    monkeypatch.delenv("ARK_PLAN_API_KEY", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        model = dict(harness="openai", model="glm-plan", url=f"http://127.0.0.1:{server.server_port}/v1",
                     key_env="ARK_PLAN_API_KEY")
        catalog = tmp_path / "models.json"
        save(catalog, dict(models=[model]))
        pool = tmp_path / "pool"; pool.mkdir(mode=0o700); (pool / "requests").mkdir()
        save(pool / "config.json", dict(runtime=dict(command=[sys.executable], files={}, version="test")))
        root = bridge.init(tmp_path / "bridge", catalog, concurrency=2, pool_root=pool)
        save(root / "config.json", dict(read(root / "config.json"), routing=dict(response_timeout_seconds=20),
                                        service=dict(models="all", stale_seconds=5)))
        spec = dict(session_id="plan-agent", dataset_id="data", prompt="Answer.", max_turns=2,
                    pool_root=str(pool), bridge_root=str(root), output_root=str(tmp_path / "session"),
                    tools=[dict(name="compute", description="Compute on a worker",
                    parameters=dict(type="object", properties=dict(value=dict(type="integer")), required=["value"], additionalProperties=False),
                    args=["-c", "raise AssertionError", "{arguments}"],
                    cpus=1, memory_mb=64, timeout_seconds=30, inputs=[], outputs=["result.json"], result_file="result.json")])
        ref = session.create_session(spec)
        saved = read(ref["path"]); saved["api_mode"] = "chat_completions"; save(ref["path"], saved); ref = reference(ref["path"])
        key = model_key(model)
        (root / "runners").mkdir()
        save(root / "runners" / (key + ".json"), dict(pid=1, generation="g", observed_at=__import__("time").time(),
                                                       in_flight=0, done=0, draining=False))
        turn = session.submit_turn(ref, 0)
        bridge.serve(root, once=True)
        assert asyncio.run(serve_runner(root, model, once=True)) == 0
        attempt = bridge.status(root, turn)["attempts"][0]
        assert read(root / "turns" / attempt["turn_id"] / "result.json")["outcome"] == "success"
        assert Provider.seen == ["Bearer fake-plan"]
    finally:
        server.shutdown()
