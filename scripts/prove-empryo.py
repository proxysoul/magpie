#!/usr/bin/env python3
import argparse
import contextlib
import http.server
import json
import os
import pathlib
import platform
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--magpie", default=shutil.which("magpie"))
    parser.add_argument("--empryo", default=shutil.which("empryo"))
    parser.add_argument("--entry")
    parser.add_argument("--output")
    args = parser.parse_args()
    if not args.magpie or not args.empryo:
        parser.error("--magpie and --empryo must name installed executables")
    magpie = str(pathlib.Path(args.magpie).resolve())
    empryo = [str(pathlib.Path(args.empryo).resolve())]
    if args.entry:
        empryo.append(str(pathlib.Path(args.entry).resolve()))
    receipt = {"platform": platform.system(), "checks": [], "requests": []}
    model = "gpt-6-luna"
    command = "echo MAGPIE_TOOL_OK" if os.name == "nt" else "printf MAGPIE_TOOL_OK"
    spawn = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def check(name, passed):
        receipt["checks"].append({"name": name, "pass": bool(passed)})
        if not passed:
            raise AssertionError(name)

    class Vendor(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_GET(self):
            body = json.dumps({"data": [{"id": model, "context_length": 1100000, "max_output_tokens": 128000}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            tool_seen = any(m.get("role") == "tool" and "MAGPIE_TOOL_OK" in str(m.get("content")) for m in body["messages"])
            receipt["requests"].append({"path": self.path, "model": body["model"], "stream": body.get("stream"), "effort": body.get("reasoning_effort"), "toolResultSeen": tool_seen})
            delta = {"content": "MAGPIE_E2E_OK"} if tool_seen else {"tool_calls": [{"index": 0, "id": "proof_shell", "type": "function", "function": {"name": "shell", "arguments": json.dumps({"command": command})}}]}
            chunks = [
                {"id": "proof", "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
                {"id": "proof", "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop" if tool_seen else "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}},
            ]
            data = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data.encode())))
            self.end_headers()
            self.wfile.write(data.encode())

    try:
        with tempfile.TemporaryDirectory(prefix="magpie-empryo-proof-") as scratch:
            root = pathlib.Path(scratch)
            home, cwd = root / "home", root / "workspace"
            cwd.mkdir()
            local = root / "local"
            env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(local), "APPDATA": str(root / "roaming"), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"), "EMPRYO_NO_KEYCHAIN": "1", "EMPRYO_E2E": "1", "EMPRYO_NO_GENOME": "1", "EMPRYO_TELEMETRY": "0", "MAGPIE_NO_ACCOUNTS": "1", "NO_COLOR": "1"}
            for key in ["CODEX_HOME", "CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME", "MAGPIE_WEB_KEY"]:
                env.pop(key, None)
            conf = (local / "Empryo" if os.name == "nt" else home / ".empryo") / "config.json"
            conf.parent.mkdir(parents=True)
            vendor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
            thread = threading.Thread(target=vendor.serve_forever)
            thread.start()
            gateway = None
            try:
                vendor_url = "http://127.0.0.1:" + str(vendor.server_port) + "/v1"
                original = {"defaultModel": "native/" + model, "providers": [{"id": "native", "baseURL": vendor_url, "models": [model]}, {"id": "magpie", "name": "User relay", "baseURL": "http://127.0.0.1:1/v1", "envVar": "USER_RELAY_KEY", "models": ["user/model"], "extraBody": {"custom": True}}], "telemetry": False, "genome": False, "tabNaming": False, "performance": {"effort": "low"}}
                conf.write_text(json.dumps(original), encoding="utf-8")
                with contextlib.closing(socket.socket()) as port:
                    port.bind(("127.0.0.1", 0))
                    env["MAGPIE_ADDR"] = "127.0.0.1:" + str(port.getsockname()[1])
                url = "http://" + env["MAGPIE_ADDR"]

                def run(words):
                    p = subprocess.run(words, cwd=cwd, env=env, capture_output=True, text=True, timeout=90, **spawn)
                    if p.returncode != 0:
                        raise RuntimeError("command failed: " + p.stdout + p.stderr)
                    return p.stdout

                def mp(*words):
                    return run([magpie, *words])

                receipt["magpie"] = mp("--version").strip()
                receipt["empryo"] = run([*empryo, "--version"]).strip()
                mp("provider", "add", "Proof", "url=" + vendor_url, "key=fixture-not-a-secret", "models=" + model, "catalog=openai")
                with (root / "gateway.log").open("w") as log:
                    gateway = subprocess.Popen([magpie, "serve"], cwd=cwd, env=env, stdout=log, stderr=log, **spawn)
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            with urllib.request.urlopen(url + "/v1", timeout=1) as response:
                                check("gateway base URL answers", response.status == 200)
                            break
                        except (urllib.error.URLError, TimeoutError, ConnectionError):
                            if gateway.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError("gateway did not become ready")
                    mp("empryo", "magpie/proof/" + model)
                    mp("empryo", "magpie/proof/" + model)
                    selected = json.loads(conf.read_text(encoding="utf-8-sig"))
                    check("correct platform config", selected["defaultModel"] == "magpie/proof/" + model)
                    check("one Magpie provider", len([p for p in selected["providers"] if p["id"] == "magpie"]) == 1)
                    check("unrelated provider preserved", selected["providers"][0] == original["providers"][0])
                    listed = run([*empryo, "--list-models", "magpie"])
                    check("Empryo discovers gateway model", model in listed)
                    prompt = "Call shell once to print MAGPIE_TOOL_OK, then answer MAGPIE_E2E_OK. Do not read or edit files."
                    output = run([*empryo, "--headless", "--events", "--no-genome", "--max-steps", "5", "--timeout", "60000", "--cwd", str(cwd), prompt])
                    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
                    done = next(e for e in events if e.get("type") == "done")
                    check("final answer", done.get("output") == "MAGPIE_E2E_OK")
                    check("real shell result", any(e.get("type") == "tool-result" and e.get("tool") == "shell" and e.get("ok") is True and "MAGPIE_TOOL_OK" in e.get("summary", "") for e in events))
                    check("tool result reaches vendor", any(r["toolResultSeen"] for r in receipt["requests"]))
                    check("streamed chat translation and effort", all(r["path"] == "/v1/chat/completions" and r["model"] == model and r["stream"] is True and r["effort"] == "low" for r in receipt["requests"]))
                    mp("empryo", "default")
                    check("restores colliding provider and default", json.loads(conf.read_text(encoding="utf-8-sig")) == original)
                    mp("empryo", "magpie/proof/" + model)
                    changed = json.loads(conf.read_text(encoding="utf-8-sig"))
                    changed["defaultModel"] = "magpie/removed/model"
                    changed["locale"] = "ja"
                    conf.write_text(json.dumps(changed), encoding="utf-8")
                    mp("empryo", "magpie/proof/" + model)
                    mp("empryo", "default")
                    restored = json.loads(conf.read_text(encoding="utf-8-sig"))
                    check("catalog change keeps original default", restored["defaultModel"] == original["defaultModel"])
                    check("unrelated config edit survives", restored["locale"] == "ja")
                    mp("empryo", "magpie/proof/" + model)
                    project = cwd / ".empryo"
                    project.mkdir(exist_ok=True)
                    (project / "config.json").write_text(json.dumps({"defaultModel": "native/" + model}), encoding="utf-8")
                    project_output = run([*empryo, "--headless", "--events", "--no-genome", "--max-steps", "5", "--timeout", "60000", "--cwd", str(cwd), prompt])
                    start = next(json.loads(line) for line in project_output.splitlines() if line.startswith("{") and json.loads(line).get("type") == "start")
                    check("project model override respected", start["model"] == "native/" + model)
                    mp("empryo", "default")
                    check("reset leaves project override alone", json.loads((project / "config.json").read_text())["defaultModel"] == "native/" + model)
            finally:
                if gateway is not None and gateway.poll() is None:
                    gateway.terminate()
                    try:
                        gateway.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        gateway.kill()
                        gateway.wait(timeout=10)
                vendor.shutdown()
                vendor.server_close()
                thread.join(timeout=10)
                if thread.is_alive():
                    raise RuntimeError("vendor thread did not stop")
    except Exception as error:
        receipt["error"] = str(error)
        raise
    finally:
        if args.output:
            pathlib.Path(args.output).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
