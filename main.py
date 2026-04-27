import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)

THINGS_API_KEY = os.environ["THINGS_API_KEY"]
APP_TOKEN = os.environ["APP_TOKEN"]

THINGS_LLM_URL = "https://things.cisco.com/api/services/llm/chat"

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _is_safe_url(url: str) -> bool:
    """Return True only for public http/https URLs (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(hostname))
        return not any(ip in net for net in _PRIVATE_NETS)
    except Exception:
        return False


def _extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(_URL_RE.findall(text)))  # dedupe, preserve order


def _fetch_and_strip(url: str) -> str:
    resp = requests.get(
        url,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Fetch/1.0)"},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                      "form", "noscript", "iframe", "banner"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _clean_with_llm(raw_text: str, url: str) -> str:
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a content extractor. Convert the following web page text into "
                    "clean, readable markdown. Keep only the main content — remove ads, "
                    "navigation, footers, cookie banners, and other boilerplate. "
                    "Preserve headings, lists, code blocks, and important links."
                ),
            },
            {
                "role": "user",
                "content": f"URL: {url}\n\n{raw_text[:12000]}",
            },
        ],
        "max_tokens": 2048,
    }
    resp = requests.post(
        THINGS_LLM_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {THINGS_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["content"]


def _require_app_token():
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {APP_TOKEN}"


@app.route("/health")
def health():
    if not _require_app_token():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"status": "ok"})


@app.route("/messages", methods=["POST"])
def messages():
    if not _require_app_token():
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True)
    if not body or body.get("type") != "webex.message":
        return "", 204

    text = body.get("data", {}).get("text", "").strip()
    urls = _extract_urls(text)

    if not urls:
        return jsonify({
            "markdown": (
                "Send me one or more URLs and I'll return clean markdown.\n\n"
                "**Example:**\n```\nhttps://example.com\nhttps://example.com/page\n```"
            )
        })

    parts = []
    for url in urls:
        if not _is_safe_url(url):
            parts.append(f"### {url}\n\n⚠️ Skipped — URL is not allowed.")
            continue
        try:
            raw = _fetch_and_strip(url)
            md = _clean_with_llm(raw, url)
            parts.append(f"### [{url}]({url})\n\n{md}")
        except requests.HTTPError as e:
            parts.append(f"### {url}\n\n❌ HTTP error: {e.response.status_code}")
        except requests.Timeout:
            parts.append(f"### {url}\n\n❌ Timed out fetching the page.")
        except Exception as e:
            parts.append(f"### {url}\n\n❌ Error: {e}")

    return jsonify({"markdown": "\n\n---\n\n".join(parts)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
