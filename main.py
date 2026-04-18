from __future__ import annotations

import argparse
import http.server
import ipaddress
import json
import subprocess
import socket
import socketserver
from pathlib import Path


VIRTUAL_ADAPTER_HINTS = (
    "vmware",
    "hyper-v",
    "wsl",
    "vethernet",
    "flclash",
    "vmnet",
)


def list_ipv4_candidates() -> list[str]:
    candidates: list[str] = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip not in candidates:
            candidates.append(ip)
    except OSError:
        pass
    finally:
        sock.close()

    return candidates


def list_windows_adapters() -> list[dict[str, str | bool]]:
    try:
        output = subprocess.check_output(
            ["ipconfig"],
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
    except (OSError, subprocess.SubprocessError):
        return []

    adapters: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] | None = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if not line.startswith(" ") and stripped.endswith(":"):
            if current:
                adapters.append(current)
            current = {"name": stripped[:-1], "ipv4": "", "gateway": "", "disconnected": False}
            continue

        if current is None:
            continue

        lower = stripped.lower()
        if "media state" in lower and "disconnected" in lower:
            current["disconnected"] = True
            continue

        if "ipv4 address" in lower:
            current["ipv4"] = stripped.split(":")[-1].strip()
            continue

        if "default gateway" in lower:
            current["gateway"] = stripped.split(":")[-1].strip()

    if current:
        adapters.append(current)

    return adapters


def rank_adapter(adapter: dict[str, str | bool]) -> tuple[int, int, int]:
    name = str(adapter["name"]).lower()
    ip = str(adapter["ipv4"])
    gateway = str(adapter["gateway"])

    virtual_penalty = 1 if any(hint in name for hint in VIRTUAL_ADAPTER_HINTS) else 0
    wireless_bonus = 1 if ("wlan" in name or "wi-fi" in name or "wireless" in name) else 0
    gateway_bonus = 1 if gateway else 0
    private_bonus = 1 if ip and ipaddress.ip_address(ip).is_private else 0

    return (virtual_penalty, -(wireless_bonus + gateway_bonus + private_bonus), 0)


def detect_lan_ip() -> str:
    """Prefer a private IPv4 address that other LAN devices can reach."""
    adapters = [
        adapter
        for adapter in list_windows_adapters()
        if adapter["ipv4"] and not adapter["disconnected"]
    ]

    ranked_adapters = sorted(adapters, key=rank_adapter)
    for adapter in ranked_adapters:
        ip = str(adapter["ipv4"])
        try:
            if ipaddress.ip_address(ip).is_private:
                return ip
        except ValueError:
            continue

    candidates = list_ipv4_candidates()

    for ip in candidates:
        try:
            if ipaddress.ip_address(ip).is_private:
                return ip
        except ValueError:
            continue

    return candidates[0] if candidates else "127.0.0.1"


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the WebXR demo on the LAN for Pico browser access."
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address, default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=8000, help="Port, default: 8000")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(__file__).resolve().parent
    lan_ip = detect_lan_ip()
    lan_url = f"http://{lan_ip}:{args.port}"
    adapter_candidates = [
        str(adapter["ipv4"])
        for adapter in sorted(
            [adapter for adapter in list_windows_adapters() if adapter["ipv4"] and not adapter["disconnected"]],
            key=rank_adapter,
        )
    ]
    fallback_candidates = [ip for ip in list_ipv4_candidates() if ip not in adapter_candidates]
    candidates = adapter_candidates + fallback_candidates
    server_info_path = root / "server-info.js"
    server_info_path.write_text(
        "window.__SERVER_INFO__ = "
        + json.dumps(
            {
                "lan_url": lan_url,
                "host": args.host,
                "port": args.port,
            },
            ensure_ascii=False,
        )
        + ";\n",
        encoding="utf-8",
    )
    handler = lambda *handler_args, **handler_kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *handler_args,
        directory=str(root),
        **handler_kwargs,
    )

    with ReusableTCPServer((args.host, args.port), handler) as httpd:
        print("VR page is available on this computer.")
        print(f"Local URL: http://localhost:{args.port}")
        print(f"LAN URL:   {lan_url}")
        if candidates:
            print("IPv4 candidates:")
            for candidate in candidates:
                print(f"  - http://{candidate}:{args.port}")
        print("Open the LAN URL in the Pico browser.")
        print("Stop with Ctrl+C.")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
