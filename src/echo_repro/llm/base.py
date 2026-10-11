from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from echo_repro.prompts import (
    build_bug_spec_extraction_prompt,
    build_dependency_repair_prompt,
    build_harness_generation_prompt,
    build_harness_repair_prompt,
    build_harness_strengthen_prompt,
)


class RequestHungError(ValueError):
    """端点接受了请求却迟迟不返回任何字节(挂死)。

    网络层在墙钟上限内收不到响应时抛出;调用方应直接失败,
    而不是走 JSON→text 回退再挂一次。
    """


def _lenient_json_loads(text: str) -> dict:
    """Parse LLM JSON output that may carry markdown fences or surrounding prose."""
    if not text:
        raise ValueError("empty text")
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, flags=re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for match in re.finditer(r"\{", candidate):
        start = match.start()
        depth = 0
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    block = candidate[start : i + 1]
                    try:
                        parsed = json.loads(block)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        # try the next '{' when this block was not balanced JSON
    raise ValueError("no JSON object found")


class BaseLLMClient(ABC):
    last_prompt: str = ""
    last_call_metadata = None

    @abstractmethod
    def generate_text(self, prompt: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def generate_json(self, prompt: str) -> dict:
        raise NotImplementedError

    def extract_bug_spec(self, issue_text: str) -> dict:
        prompt = build_bug_spec_extraction_prompt(issue_text)
        self.last_prompt = prompt
        return self.generate_json(prompt)

    def generate_harness(self, concise_context: str) -> str:
        prompt = build_harness_generation_prompt(concise_context)
        self.last_prompt = prompt
        return self.generate_text(prompt)

    def repair_harness(self, concise_context: str, current_code: str, feedback: str) -> str:
        prompt = build_harness_repair_prompt(
            concise_context=concise_context,
            current_code=current_code,
            feedback=feedback,
        )
        self.last_prompt = prompt
        return self.generate_text(prompt)

    def strengthen_oracle(self, concise_context: str, current_code: str, feedback: str) -> str:
        prompt = build_harness_strengthen_prompt(
            concise_context=concise_context,
            current_code=current_code,
            feedback=feedback,
        )
        self.last_prompt = prompt
        return self.generate_text(prompt)

    def repair_dependency(
        self,
        repo_slug: str,
        missing_module: str,
        stderr_tail: str,
        python_version: str,
        declaration_summary: str,
        feedback: str = "",
        installed_packages: str = "",
    ) -> dict:
        prompt = build_dependency_repair_prompt(
            repo_slug=repo_slug,
            missing_module=missing_module,
            stderr_tail=stderr_tail,
            python_version=python_version,
            declaration_summary=declaration_summary,
            feedback=feedback,
            installed_packages=installed_packages,
        )
        self.last_prompt = prompt
        try:
            return self.generate_json(prompt)
        except RequestHungError:
            raise
        except ValueError:
            # some clients raise JSONDecodeError before the raw text is visible;
            # fall back to text generation and lenient parsing
            return _lenient_json_loads(self.generate_text(prompt))
