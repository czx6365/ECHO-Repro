from __future__ import annotations

import re


def clean_generated_python(code: str) -> str:
    text = code.strip()

    fenced = re.fullmatch(r"```(?:python|py)?\s*\n(?P<body>.*?)\n```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group("body").strip()
    else:
        text = re.sub(r"^\s*```(?:python|py)?\s*$", "", text, flags=re.IGNORECASE | re.MULTILINE)
        text = re.sub(r"^\s*```\s*$", "", text, flags=re.MULTILINE).strip()

    return text + "\n"
