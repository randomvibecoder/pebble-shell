from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from . import __version__


class ExaSearchClient:
    def __init__(self, api_key: str, base_url: str = "https://api.exa.ai") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, num_results: int = 5) -> dict[str, Any]:
        if not self.api_key:
            raise ValueError("EXA_API_KEY is required for websearch")
        query = query.strip()
        if not query:
            raise ValueError("Exa query cannot be empty")
        payload = {
            "query": query,
            "numResults": max(1, min(num_results, 10)),
        }
        request = urllib.request.Request(
            f"{self.base_url}/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "user-agent": f"PebbleShell/{__version__}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Exa search failed: HTTP {exc.code}: {detail}") from exc
