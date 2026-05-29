from echo_repro.prompts import (
    build_harness_generation_prompt,
    build_harness_repair_prompt,
    build_harness_strengthen_prompt,
)


def test_harness_prompts_include_oracle_contract_and_traceback_guidance():
    prompts = [
        build_harness_generation_prompt("Context"),
        build_harness_repair_prompt("Context", 'print("Other issues")', "feedback"),
        build_harness_strengthen_prompt("Context", 'print("Other issues")', "feedback"),
    ]

    for prompt in prompts:
        assert "Oracle contract" in prompt
        assert "traceback.print_exc()" in prompt
        assert "Print exactly one final stdout line" in prompt
        assert "smallest issue-specific observation" in prompt or "narrow failure signature" in prompt
