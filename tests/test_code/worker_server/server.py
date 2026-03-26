from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"hello from server\n")


if __name__ == "__main__":
    # Listen on 0.0.0.0:8000
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
