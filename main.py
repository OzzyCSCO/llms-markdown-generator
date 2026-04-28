import io
import ipaddress
import os
import re
import socket
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse

import boto3
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)

THINGS_API_KEY = os.environ["THINGS_API_KEY"]
APP_TOKEN = os.environ["APP_TOKEN"]
S3_BUCKET = os.environ["S3_BUCKET"]
S3_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

THINGS_LLM_URL = "https://things.cisco.com/api/services/llm/chat"
THINGS_WEBEX_URL = "https://things.cisco.com/api/services/webex/message"

s3 = boto3.client("s3", region_name=S3_REGION)

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


def _url_to_filename(url: str) -> str:
    """Convert a URL to a safe .md filename."""
    parsed = urlparse(url)
    name = (parsed.netloc + parsed.path).strip("/").replace("/", "-")
    name = re.sub(r"[^\w\-]", "-", name).strip("-") or "page"
    return f"{name}.md"


def _build_zip(results: list[tuple[str, str]]) -> bytes:
    """Build an in-memory zip containing one .md file per URL."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        seen: dict[str, int] = {}
        for url, content in results:
            base = _url_to_filename(url)
            if base in seen:
                seen[base] += 1
                stem, ext = base.rsplit(".", 1)
                name = f"{stem}-{seen[base]}.{ext}"
            else:
                seen[base] = 0
                name = base
            zf.writestr(name, content)
    return buf.getvalue()


def _upload_and_presign(zip_bytes: bytes) -> str:
    """Upload zip to S3 and return a 24-hour presigned URL."""
    key = f"fetch-bot/{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=zip_bytes,
        ContentType="application/zip",
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=86400,  # 24 hours
    )


def _send_webex_message(room_id: str, text: str, file_url: str | None = None):
    """Send an outbound Webex message via Things API."""
    payload: dict = {"room_id": room_id, "markdown": text}
    if file_url:
        payload["files"] = [file_url]
    try:
        requests.post(
            THINGS_WEBEX_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {THINGS_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        app.logger.warning("Outbound Webex send failed: %s", e)


@app.route("/messages", methods=["POST"])
def messages():
    if not _require_app_token():
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True)
    if not body or body.get("type") != "webex.message":
        return "", 204

    data = body.get("data", {})
    text = data.get("text", "").strip()
    room_id = data.get("room_id", "")
    urls = _extract_urls(text)

    if not urls:
        _send_webex_message(
            room_id,
            "Send me one or more URLs and I'll return a zip of clean `.md` files.\n\n"
            "**Example:**\n```\nhttps://example.com\nhttps://example.com/page\n```",
        )
        return "", 204

    # Acknowledge immediately — processing may take a while
    _send_webex_message(
        room_id,
        f"Fetching **{len(urls)} URL{'s' if len(urls) > 1 else ''}**... I'll send the zip when it's ready.",
    )

    results: list[tuple[str, str]] = []
    errors: list[str] = []

    for url in urls:
        if not _is_safe_url(url):
            errors.append(f"⚠️ `{url}` — skipped (not allowed)")
            continue
        try:
            raw = _fetch_and_strip(url)
            md = _clean_with_llm(raw, url)
            results.append((url, md))
        except requests.HTTPError as e:
            errors.append(f"❌ `{url}` — HTTP {e.response.status_code}")
        except requests.Timeout:
            errors.append(f"❌ `{url}` — timed out")
        except Exception as e:
            errors.append(f"❌ `{url}` — {e}")

    if results:
        zip_bytes = _build_zip(results)
        presigned_url = _upload_and_presign(zip_bytes)
        summary = f"Done! **{len(results)} file{'s' if len(results) > 1 else ''}** ready (link expires in 24h)."
        if errors:
            summary += "\n\n" + "\n".join(errors)
        _send_webex_message(room_id, summary, file_url=presigned_url)
    else:
        _send_webex_message(room_id, "All URLs failed:\n\n" + "\n".join(errors))

    return "", 204


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
