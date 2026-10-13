"""Loopback-only keep-alive model stub; no real API or secret is used."""

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chatbot import schemas
from chatbot.auto_routing import RoutingDecision


@contextmanager
def model_http(fake):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = body["messages"]
            calls.append({"connection_port": self.client_address[1], "model": body["model"]})
            schema = None
            if body.get("response_format"):
                document = json.loads(messages[-1]["content"].split("：", 1)[1])
                schema = RoutingDecision if document["title"] == "RoutingDecision" else getattr(schemas, document["title"])
            roles = {"ActorIntent": "role_intent", "ActorSpeech": "dialogue",
                     "ActorReview": "actor_review", "RoleReflection": "role_memory"}
            role = roles.get(schema.__name__, "dialogue") if schema else "dialogue"
            if schema is RoutingDecision:
                data = json.loads(messages[-2]["content"])
                value = {"assessments": [{"edge_id": edge["id"], "status": "not_met",
                                          "reason": "继续核对传闻", "evidence": []}
                                         for edge in data["exits"]]}
            else:
                value = asyncio.run(fake.chat("loopback-stub", role, messages, schema=schema))
            if schema:
                if schema.__name__ == "ActorReview":
                    value["delivery"] = {}
                if schema.__name__ == "ActorIntent":
                    value["retrieval"] = "context"
                value = json.dumps(schema.model_validate(value).model_dump(), ensure_ascii=False)
            usage = {"prompt_tokens": 20, "completion_tokens": 10}
            if body.get("stream"):
                frames = [{"choices": [{"delta": {"content": value}}]},
                          {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                          {"choices": [], "usage": usage}]
                encoded = ("".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
                           + "data: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                encoded = json.dumps({"choices": [{"message": {"content": value},
                                                  "finish_reason": "stop"}], "usage": usage}).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
