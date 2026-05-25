from echo_repro.code_cleaner import clean_generated_python
from echo_repro.harness_generator import generate_harness
from echo_repro.llm.mock_client import MockLLMClient


class FencedMockLLM(MockLLMClient):
    def generate_harness(self, concise_context: str) -> str:
        return """```python
print("Issue reproduced")
```"""


def test_clean_generated_python_removes_markdown_fence():
    assert clean_generated_python("""```python
print("Issue reproduced")
```""") == 'print("Issue reproduced")\n'


def test_generate_harness_cleans_fenced_llm_output():
    harness = generate_harness("context", FencedMockLLM())
    assert harness.code == 'print("Issue reproduced")\n'
