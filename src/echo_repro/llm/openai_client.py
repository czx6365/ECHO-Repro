from __future__ import annotations

import json
from urllib import request

from echo_repro.config import get_llm_settings
from echo_repro.llm.base import BaseLLMClient


class OpenAICompatibleLLMClient(BaseLLMClient):
    def __init__(self) -> None:
        self.settings = get_llm_settings()
        if not self.settings.base_url or not self.settings.api_key:
            raise ValueError(
                "OpenAI-compatible client requires ECHO_REPRO_LLM_BASE_URL and ECHO_REPRO_LLM_API_KEY."
            )

    def _post_chat(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=f"{self.settings.base_url.rstrip('/')}/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.api_key}",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=self.settings.timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]

    def extract_bug_spec(self, issue_text: str) -> dict:
        content = self._post_chat(
            "You extract structured bug specifications as compact JSON.",
            (
                "Return JSON with keys: title, summary, current_behavior, expected_behavior, "
                "failure_signature, reproduction_hint, keywords, suspect_symbols.\n\n"
                f"Issue text:\n{issue_text}"
            ),
        )
        return json.loads(content)

    def generate_harness(self, concise_context: str) -> str:
        return self._post_chat(
            "You write minimal executable Python reproduction harnesses.",
            (
                "Generate only a complete Python file. It must print exactly one of "
                "'Issue reproduced', 'Issue resolved', or 'Other issues'.\n\n"
                f"Context:\n{concise_context}"
            ),
        )

    def repair_harness(self, concise_context: str, current_code: str, feedback: str) -> str:
        return self._post_chat(
            "You repair failing Python reproduction harnesses.",
            (
                "Repair the Python harness based on the execution feedback. "
                "Return only a complete Python file. It must print exactly one of "
                "'Issue reproduced', 'Issue resolved', or 'Other issues'.\n\n"
                f"Context:\n{concise_context}\n\n"
                f"Current harness:\n{current_code}\n\n"
                f"Feedback:\n{feedback}"
            ),
        )

    def strengthen_oracle(self, concise_context: str, current_code: str, feedback: str) -> str:
        return self._post_chat(
            "You strengthen bug reproduction harness oracles.",
            (
                "Revise the Python harness so it better distinguishes reproduced bugs from resolved behavior. "
                "Return only a complete Python file. It must print exactly one of "
                "'Issue reproduced', 'Issue resolved', or 'Other issues'.\n\n"
                f"Context:\n{concise_context}\n\n"
                f"Current harness:\n{current_code}\n\n"
                f"Feedback:\n{feedback}"
            ),
        )
