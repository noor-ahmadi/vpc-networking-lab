#!/usr/bin/env python3
"""Small HTTP service that queries PostgreSQL over the isolated subnet."""

from http.server import BaseHTTPRequestHandler
import json
import os
from socketserver import TCPServer

import psycopg2


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status = 200
        if self.path == "/health":
            body = {"status": "ok"}
        elif self.path == "/message":
            try:
                with psycopg2.connect(
                    host="10.0.3.10", port=5432, dbname="vpc_lab", user="vpc_app",
                    password=os.environ["VPC_DB_PASSWORD"], connect_timeout=2,
                    options="-c statement_timeout=2000", sslmode="disable",
                ) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT message FROM demo_message WHERE id = 1")
                        body = {"message": cursor.fetchone()[0]}
            except psycopg2.Error:
                status, body = 503, {"error": "database unavailable"}
        else:
            status, body = 404, {"error": "not found"}
        body["peer"] = self.client_address[0]
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    TCPServer.allow_reuse_address = True
    with TCPServer(("10.0.2.10", 8080), Handler) as server:
        server.serve_forever()
