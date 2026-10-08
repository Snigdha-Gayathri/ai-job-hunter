#!/usr/bin/env python3
"""
Apify Authentication Verification Diagnostic

Safely verifies configured Apify credentials against:
GET https://api.apify.com/v2/users/me

Reports:
  APIFY AUTH: VALID (Account: <username>)
  or
  APIFY AUTH: INVALID (<reason>)

CRITICAL SECURITY CONSTRAINT:
Never exposes, logs, prints, or echoes the actual API token anywhere.
"""

import os
import sys
import requests


def get_clean_apify_token(token: str | None = None) -> str:
    """
    Safely retrieves and sanitizes the Apify API token from the environment.
    Supports APIFY_API_TOKEN, APIFY_TOKEN, and APIFY_API_KEY.
    Strips leading/trailing whitespace, newlines, surrounding quotes, and 'Bearer ' prefix.
    Never prints or exposes the token value.
    """
    if token is not None:
        raw = token
    else:
        raw = (
            os.getenv("APIFY_API_KEY")
            or os.getenv("APIFY_API_TOKEN")
            or os.getenv("APIFY_TOKEN")
            or ""
        )
    cleaned = raw.strip().strip("'\"")
    if cleaned.startswith("Bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned


def verify_apify_auth(token: str | None = None) -> tuple[bool, str, dict]:
    """
    Safely verifies the Apify API token against GET https://api.apify.com/v2/users/me.
    Never logs or prints the token value.

    Returns:
        (is_valid: bool, status: str, details: dict)
    """
    resolved = get_clean_apify_token(token)
    if not resolved:
        print("APIFY AUTH: INVALID (No Apify API token configured in environment)")
        return False, "MISSING_TOKEN", {"error": "Token not configured in environment"}

    url = "https://api.apify.com/v2/users/me"
    headers = {
        "Authorization": f"Bearer {resolved}",
        "Content-Type": "application/json",
    }
    params = {"token": resolved}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            try:
                data = resp.json().get("data", {})
                username = data.get("username") or data.get("id") or "verified_account"
            except Exception:
                username = "verified_account"
            print(f"APIFY AUTH: VALID (Account: {username})")
            return True, "VALID", {"status_code": 200, "username": username}

        elif resp.status_code == 401:
            print("APIFY AUTH: INVALID (HTTP 401: User was not found or authentication token is not valid)")
            return False, "INVALID_TOKEN", {"status_code": 401, "error": "user-or-token-not-found"}

        elif resp.status_code == 403:
            print("APIFY AUTH: VALID (HTTP 403: Token is authentic, but account has usage limitations / platform restrictions)")
            return True, "USAGE_LIMITED", {"status_code": 403, "error": "usage-limited"}

        else:
            print(f"APIFY AUTH: INVALID (HTTP {resp.status_code})")
            return False, f"HTTP_{resp.status_code}", {"status_code": resp.status_code}

    except requests.RequestException as exc:
        print(f"APIFY AUTH: INVALID (Network connection error: {type(exc).__name__})")
        return False, "NETWORK_ERROR", {"error": str(exc)}


def main():
    token = get_clean_apify_token()
    if not token:
        print("APIFY AUTH: INVALID (No Apify API token configured in environment)")
        print("Expected environment variable: APIFY_API_TOKEN (or APIFY_TOKEN)")
        sys.exit(1)

    is_valid, status, _ = verify_apify_auth(token)
    if is_valid:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
