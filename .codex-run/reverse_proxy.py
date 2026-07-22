from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client


BACKEND_PORT = 8000
FRONTEND_PORT = 3001
API_PREFIXES = (
    "/health",
    "/extract",
    "/contracheque",
    "/preview",
)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_OPTIONS(self):
        self._proxy()

    def _proxy(self):
        target_port = BACKEND_PORT if self.path.startswith(API_PREFIXES) else FRONTEND_PORT
        body_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(body_length) if body_length else None

        headers = {key: value for key, value in self.headers.items()}
        headers["Host"] = f"localhost:{target_port}"
        headers.pop("Accept-Encoding", None)

        conn = http.client.HTTPConnection("localhost", target_port, timeout=300)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            response = conn.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                lower = key.lower()
                if lower in {"transfer-encoding", "connection", "content-encoding"}:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()

            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            conn.close()

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8787), ProxyHandler)
    server.serve_forever()
