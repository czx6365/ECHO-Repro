from echo_repro.prompts import (
    build_dependency_repair_prompt,
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


def test_dependency_repair_prompt_includes_context_and_contract():
    prompt = build_dependency_repair_prompt(
        repo_slug="django/django",
        missing_module="pytz",
        stderr_tail="ModuleNotFoundError: No module named 'pytz'",
        python_version="Python 3.11.9",
        declaration_summary="setup.py:12: pytz",
    )

    assert "pytz" in prompt
    assert "django/django" in prompt
    assert "Python 3.11.9" in prompt
    assert "setup.py:12: pytz" in prompt
    assert "ModuleNotFoundError" in prompt
    assert "packages" in prompt and "install_commands" in prompt and "reason" in prompt
    assert "Do not include markdown fences" in prompt
    assert "--user" in prompt and "--break-system-packages" in prompt
    assert "pip install -e ." in prompt
    assert "shadows" in prompt
    assert "Previous install attempt failed" not in prompt


def test_dependency_repair_prompt_includes_retry_feedback():
    prompt = build_dependency_repair_prompt(
        repo_slug="sympy/sympy",
        missing_module="mpmath",
        stderr_tail="boom",
        python_version="Python 3.11",
        declaration_summary="(none)",
        feedback="ERROR: Could not find a version that satisfies the requirement mpmath==99",
    )

    assert "Previous install attempt failed" in prompt
    assert "mpmath==99" in prompt


def test_dependency_repair_prompt_guides_pkg_resources_pin_and_freeze():
    prompt = build_dependency_repair_prompt(
        repo_slug="pydata/xarray",
        missing_module="pkg_resources",
        stderr_tail="No module named 'pkg_resources'",
        python_version="Python 3.12",
        declaration_summary="(none)",
        installed_packages="setuptools==84.0.0\nnumpy==1.26.4",
    )

    assert "setuptools<81" in prompt
    assert "setuptools==84.0.0" in prompt
    assert "installed version is probably wrong" in prompt
