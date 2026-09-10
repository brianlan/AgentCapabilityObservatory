"""Local mock Ark Agent Plan endpoint for pi e2e tests (#37).

Speaks just enough OpenAI-Responses SSE for pi 0.84.1: turn 1 requests a bash
tool call that writes the answer file, turn 2 confirms. No real key is ever
needed — the Authorization header is recorded so tests can assert injection
without ever containing a real secret.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ANSWER_TEXT = "PI-E2E-OK"


class MockArk:
    """Threaded mock; scenario switches between 'tool_call' and 'auth_fail'."""

    def __init__(self):
        self.requests = []  # {"path","auth","model"} per POST
        self.scenario = "tool_call"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append({"path": self.path,
                                       "auth": self.headers.get("Authorization"),
                                       "model": body.get("model")})
                if outer.scenario == "auth_fail":
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": {"message": "Invalid API key provided"}}')
                    return
                model = body.get("model", "unknown")
                has_tool_output = any(
                    isinstance(item, dict) and item.get("type") == "function_call_output"
                    for item in (body.get("input") or [])
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(payload, flush=True):
                    self.wfile.write(payload)
                    if flush:
                        self.wfile.flush()

                def sse(event):
                    send(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

                if not has_tool_output:
                    call = {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                            "name": "bash",
                            "arguments": json.dumps(
                                {"command": f"printf '%s' '{ANSWER_TEXT}' > /workspace/answer.txt"})}
                    sse({"type": "response.created", "response": {"id": "resp_1", "model": model}})
                    sse({"type": "response.output_item.added", "output_index": 0, "item": call})
                    sse({"type": "response.output_item.done", "output_index": 0, "item": call})
                    sse({"type": "response.completed", "response": {
                        "id": "resp_1", "model": model,
                        "usage": {"input_tokens": 20, "output_tokens": 9, "total_tokens": 29},
                        "output": [call]}})
                else:
                    msg = {"type": "message", "role": "assistant", "id": "msg_2",
                           "content": [{"type": "output_text", "text": ANSWER_TEXT}]}
                    sse({"type": "response.created", "response": {"id": "resp_2", "model": model}})
                    sse({"type": "response.output_item.added", "output_index": 0,
                         "item": {"type": "message", "role": "assistant", "id": "msg_2",
                                  "content": []}})
                    sse({"type": "response.output_item.done", "output_index": 0, "item": msg})
                    sse({"type": "response.completed", "response": {
                        "id": "resp_2", "model": model,
                        "usage": {"input_tokens": 40, "output_tokens": 6, "total_tokens": 46},
                        "output": [msg]}})

        self.server = HTTPServer(("0.0.0.0", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server.server_port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
