"""Verify OTLP HTTP trace export using a temporary local receiver and API container."""

import http.server
import socket
import subprocess
import threading
import time
import urllib.request

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    received: list[int] = []

    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/traces":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers["Content-Length"]))
            export = ExportTraceServiceRequest()
            export.ParseFromString(body)
            count = sum(
                len(scope.spans)
                for resource in export.resource_spans
                for scope in resource.scope_spans
            )
            received.append(count)
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    collector = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Receiver)
    collector_port = collector.server_address[1]
    thread = threading.Thread(target=collector.serve_forever, daemon=True)
    thread.start()
    api_port = free_port()
    command = [
        "docker",
        "compose",
        "run",
        "--rm",
        "--no-deps",
        "-p",
        f"127.0.0.1:{api_port}:8000",
        "-e",
        f"OTLP_ENDPOINT=http://host.docker.internal:{collector_port}/v1/traces",
        "api",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"API container exited: {process.returncode}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{api_port}/health/live", timeout=1):
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Temporary API did not start")
        for _ in range(3):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{api_port}/health/ready", timeout=5
            ) as response:
                assert response.status == 200
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not received:
            time.sleep(0.25)
        if not received or sum(received) < 1:
            raise RuntimeError("No OTLP spans received")
        print(f"OTLP PASS: {sum(received)} spans in {len(received)} export request(s)")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        collector.shutdown()
        collector.server_close()


if __name__ == "__main__":
    main()
