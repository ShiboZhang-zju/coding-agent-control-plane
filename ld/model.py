"""Real-model DecisionModel implementations for the LD (M4-A/M4-B).

The model boundary is deliberately narrow: ``decide()`` receives the
security-domain-filtered decision input built by
:meth:`ld.coordinator.LDCoordinator._decision_input` and must return one
JSON-shaped mapping.  It never receives credentials, never talks to the Worker,
the ledger, or the filesystem, and its output is always re-validated by
:class:`ld.decision.LDDecision` before anything executes.

``OpenAIChatModel`` speaks an OpenAI-compatible ``/chat/completions`` endpoint
with the standard library only — no provider SDK, no agent framework.  The
endpoint, credentials and authorized security domains are explicit
configuration; nothing is sent anywhere by default.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

_SYSTEM_PROMPT = (
    "You are the decision core of a local development coordinator (LD). "
    "Read the JSON decision input and reply with EXACTLY ONE JSON object "
    "following the embedded decision_schema — exactly the five top-level "
    "fields (action, project_id, reason, evidence_refs, parameters) and "
    "nothing else. No prose, no markdown fences, no code, no extra keys.\n"
    "Critical format rules:\n"
    "- parameters.assertions is an ARRAY OF OBJECTS with kind and target "
    "(and value for file_contains: target is the file path, value is the "
    "text it must contain). kind is one of file_exists, file_contains, "
    "git_commit, git_clean, path_absent. Never strings.\n"
    "- Do not invent task ids, project ids, verdicts or evidence refs that "
    "are not present in the input.\n"
    "- If you cannot act safely, use a request_human decision.\n"
    "Example submit_task decision:\n"
    "{\"action\":\"submit_task\",\"project_id\":\"prj-123\","
    "\"reason\":\"first step toward the goal\","
    "\"evidence_refs\":[\"task:cp-1\"],"
    "\"parameters\":{\"task\":\"create file X with content Y\","
    "\"idempotency_key\":\"goal-step-1\","
    "\"assertions\":[{\"kind\":\"file_exists\",\"target\":\"X.txt\"},"
    "{\"kind\":\"file_contains\",\"target\":\"X.txt\",\"value\":\"Y\"}]}}"
)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class ModelCallError(RuntimeError):
    """A real model call failed (transport, HTTP, timeout, bad payload)."""


class ModelTimeoutError(ModelCallError):
    """The model call exceeded its configured deadline."""


class ModelResponseError(ModelCallError):
    """The endpoint answered but the content is not one JSON object."""


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


class OpenAIChatModel:
    """One structured decision per call against an OpenAI-compatible endpoint.

    ``base_url`` may be either the full chat-completions URL or an API root
    such as ``https://host/v1`` (the ``/chat/completions`` suffix is appended
    when missing).  ``api_key`` is optional for local endpoints that do not
    authenticate.  Nothing about the endpoint or key is logged.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        max_tokens: int = 4096,
        temperature: float | None = None,
        extra_headers: Mapping[str, str] | None = None,
        system_prompt: str = _SYSTEM_PROMPT,
    ):
        if not base_url or not isinstance(base_url, str):
            raise ValueError("base_url must be a non-empty string")
        if not model or not isinstance(model, str):
            raise ValueError("model must be a non-empty string")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        base = base_url.rstrip("/")
        if not base.endswith("/chat/completions"):
            base = base + "/chat/completions"
        self._url = base
        self._model = model
        self._api_key = api_key
        self._timeout_s = float(timeout_s)
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._extra_headers = dict(extra_headers or {})
        self._system_prompt = system_prompt
        # Host + model name only; never credentials, prompts or responses.
        self._log_host = urllib.parse.urlsplit(self._url).netloc

    @classmethod
    def from_env(cls) -> "OpenAIChatModel | None":
        """Configure from LD_MODEL_* environment variables, or None."""
        base_url = os.environ.get("LD_MODEL_BASE_URL", "").strip()
        model = os.environ.get("LD_MODEL_NAME", "").strip()
        if not base_url or not model:
            return None
        timeout_raw = os.environ.get("LD_MODEL_TIMEOUT_S", "").strip()
        return cls(
            base_url=base_url,
            model=model,
            api_key=os.environ.get("LD_MODEL_API_KEY") or None,
            timeout_s=float(timeout_raw) if timeout_raw else 60.0,
        )

    def describe(self) -> dict[str, Any]:
        """Non-secret endpoint description, safe for evidence and logs."""
        return {"endpoint_host": self._log_host, "model": self._model,
                "timeout_s": self._timeout_s, "max_tokens": self._max_tokens}

    def decide(self, decision_input: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return one JSON-shaped decision; raise ModelCallError on failure."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user",
                 "content": json.dumps(decision_input, ensure_ascii=False,
                                       default=str)},
            ],
            "max_tokens": self._max_tokens,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json", **self._extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        request = urllib.request.Request(
            self._url, data=body, headers=headers, method="POST")
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            snippet = ""
            try:
                snippet = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
            raise ModelCallError(
                f"model endpoint returned HTTP {exc.code}: {snippet}") from exc
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise ModelTimeoutError(
                    f"model call timed out after {self._timeout_s}s") from exc
            raise ModelCallError(f"model endpoint unreachable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ModelTimeoutError(
                f"model call timed out after {self._timeout_s}s") from exc
        finally:
            # Duration only — never the payload or the response.
            self.last_call_s = round(time.monotonic() - started, 3)

        try:
            decoded = json.loads(raw)
            content = decoded["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ModelResponseError(
                f"model response is not a chat completion: {type(exc).__name__}"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ModelResponseError("model returned empty content")
        try:
            decision = json.loads(_strip_fences(content))
        except json.JSONDecodeError as exc:
            raise ModelResponseError(
                f"model content is not valid JSON: {exc}") from exc
        if not isinstance(decision, Mapping):
            raise ModelResponseError("model content must be one JSON object")
        return decision


__all__ = [
    "ModelCallError",
    "ModelResponseError",
    "ModelTimeoutError",
    "OpenAIChatModel",
]
