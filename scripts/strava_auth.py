"""
scripts/strava_auth.py — One-time Strava OAuth setup.

Run this once locally to obtain your refresh token:
    python scripts/strava_auth.py

Then copy the printed refresh token into your .env (or GitHub secret).

Prerequisites:
  1. Create a Strava API application at https://www.strava.com/settings/api
  2. Set the "Authorization Callback Domain" to "localhost"
  3. Copy the Client ID and Client Secret into your .env
"""

import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ["STRAVA_CLIENT_ID"]
CLIENT_SECRET = os.environ["STRAVA_CLIENT_SECRET"]
REDIRECT_URI  = "http://localhost:8765/callback"

auth_code_holder: list[str] = []


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code   = params.get("code", [None])[0]

        if code:
            auth_code_holder.append(code)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Authorization successful. You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>No code received.</h2>")

    def log_message(self, format, *args):  # noqa: A002
        pass  # Suppress access log noise


def main():
    auth_url = (
        f"https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&approval_prompt=force"
        f"&scope=activity:read_all,activity:write"
    )

    print("Opening browser for Strava authorization …")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8765), CallbackHandler)
    server.timeout = 120
    print("Waiting for callback on http://localhost:8765/callback …")
    server.handle_request()

    if not auth_code_holder:
        print("ERROR: No authorization code received. Did you approve access?")
        return

    code = auth_code_holder[0]

    resp = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code":          code,
            "grant_type":    "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    refresh_token = data["refresh_token"]
    athlete       = data.get("athlete", {})

    print("\n" + "=" * 60)
    print(f"Authorized as: {athlete.get('firstname', '')} {athlete.get('lastname', '')}")
    print(f"Athlete ID:    {athlete.get('id', 'unknown')}")
    print()
    print("Add this to your .env (or GitHub Actions secret):")
    print(f"  STRAVA_REFRESH_TOKEN={refresh_token}")
    print("=" * 60)


if __name__ == "__main__":
    main()
