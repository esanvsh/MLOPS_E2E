#!/usr/bin/env python3
"""Receives Alertmanager webhooks and prints them to stdout with colours."""

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 9999

RED   = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD  = "\033[1m"


class AlertHandler(BaseHTTPRequestHandler):

    def do_POST(self):
        if self.path != "/alert":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        self._print_payload(payload)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def _print_payload(self, payload):
        status = payload.get("status", "unknown")
        alerts = payload.get("alerts", [])

        for alert in alerts:
            name    = alert.get("labels", {}).get("alertname", "unknown")
            summary = alert.get("annotations", {}).get("summary", "")
            desc    = alert.get("annotations", {}).get("description", "")
            sev     = alert.get("labels", {}).get("severity", "unknown")
            starts  = alert.get("startsAt", "")

            if starts:
                try:
                    starts = datetime.fromisoformat(starts.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S UTC")
                except ValueError:
                    pass

            print()
            if status == "firing":
                print(f"{RED}{BOLD}🚨 ALERT FIRING: {name} — {summary}{RESET}")
            elif status == "resolved":
                print(f"{GREEN}{BOLD}✅ ALERT RESOLVED: {name}{RESET}")
            else:
                print(f"{YELLOW}{BOLD}❓ ALERT {status.upper()}: {name}{RESET}")

            print(f"   severity  : {sev}")
            if desc:
                print(f"   description: {desc}")
            if starts:
                print(f"   starts_at : {starts}")

    def log_message(self, fmt, *args):
        # Suppress default access log noise; errors still surface via log_error.
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), AlertHandler)
    print(f"🎧 Alert webhook listener started on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
