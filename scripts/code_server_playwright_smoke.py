#!/usr/bin/env python3
"""Deterministic Playwright smoke for Kubernetes-hosted code-server."""

import json
from playwright.sync_api import sync_playwright


URL = "http://s0-code-server.saasbench.localhost:30090/login"
PASSWORD = "8a128206e2177bce1e48e565"


def main() -> None:
    result = {
        "app": "code-server",
        "target": URL,
        "status": "FAIL",
        "login_url": URL,
        "final_url": "",
        "title": "",
        "password_input": False,
        "page_text": "",
        "error": None,
    }
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--host-resolver-rules=MAP s0-code-server.saasbench.localhost 127.0.0.1"],
            )
            page = browser.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1000)
            result["final_url"] = page.url
            result["title"] = page.title()
            password_input = page.locator("input[type=password]").count() > 0
            result["password_input"] = password_input
            if password_input:
                page.locator("input[type=password]").first.fill(PASSWORD)
                page.keyboard.press("Enter")
                page.wait_for_timeout(3000)
                result["final_url"] = page.url
                result["title"] = page.title()
            result["page_text"] = " ".join(page.locator("body").inner_text().split())[:300]
            body = result["page_text"].lower()
            result["status"] = "PASS" if ("code-server" in body or "workspace" in body or password_input) else "FAIL"
            browser.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
