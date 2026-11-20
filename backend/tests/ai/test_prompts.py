from __future__ import annotations

import os
from pathlib import Path

import yaml

from app.ai.prompts import (
    DEFAULT_PROFILES,
    PromptProfile,
    PromptsManager,
    PromptsState,
)


def test_seeds_default_prompts_yaml_on_first_boot(tmp_path: Path) -> None:
    prompts_path = tmp_path / "config" / "prompts.yaml"
    assert not prompts_path.exists()

    manager = PromptsManager(prompts_path=prompts_path)
    state: PromptsState = manager.load_or_fallback()

    assert prompts_path.is_file()
    assert state.syntax_warning is None
    assert state.error_line is None

    # Verify required academic and reviewer profiles exist
    profiles = state.profiles
    assert "writing" in profiles
    assert "synthesis" in profiles
    assert "proofreading" in profiles
    assert "formal_tone" in profiles
    assert "tighten" in profiles
    assert "verify_claim" in profiles
    assert "explain_math" in profiles
    assert "fix_latex" in profiles
    assert "compile_autofix" in profiles

    reviewer_names = {
        p.name for p in profiles.values() if p.role == "reviewer"
    }
    assert "Methodology & Logic Critic" in reviewer_names
    assert "Literature & Grounding Auditor" in reviewer_names
    assert "Structure & Clarity Editor" in reviewer_names

    for prof in profiles.values():
        assert isinstance(prof, PromptProfile)
        assert prof.id
        assert prof.name
        assert prof.system_prompt.strip() != ""


def test_invalid_yaml_retains_last_valid_config_and_reports_line_number(
    tmp_path: Path,
) -> None:
    prompts_path = tmp_path / "config" / "prompts.yaml"
    manager = PromptsManager(prompts_path=prompts_path)

    initial_state = manager.load_or_fallback()
    assert initial_state.syntax_warning is None
    assert "writing" in initial_state.profiles

    # Modify a valid profile on disk and verify mtime hot-reload
    valid_custom_yaml = """profiles:
  writing:
    name: "Custom Academic Writer"
    role: "writing"
    system_prompt: "Write rigorous academic prose."
  compile_autofix:
    name: "Compile Error Fixer"
    role: "compile_autofix"
    system_prompt: "Fix LaTeX compile errors minimally."
"""
    prompts_path.write_text(valid_custom_yaml, encoding="utf-8")
    # Bump mtime explicitly to guarantee st_mtime_ns change on fast filesystems
    st = prompts_path.stat()
    os.utime(prompts_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    reloaded = manager.load_or_fallback()
    assert reloaded.syntax_warning is None
    assert reloaded.error_line is None
    assert reloaded.profiles["writing"].name == "Custom Academic Writer"

    # Now write invalid YAML on disk with a syntax error on line 4
    broken_yaml = (
        "profiles:\n"
        "  writing:\n"
        '    name: "Broken Profile"\n'
        "    role: [unclosed_list\n"
        '    system_prompt: "Oops"\n'
    )
    prompts_path.write_text(broken_yaml, encoding="utf-8")
    st2 = prompts_path.stat()
    os.utime(prompts_path, ns=(st2.st_atime_ns, st2.st_mtime_ns + 2_000_000))

    fallback_state = manager.load_or_fallback()
    # Prior valid profiles are retained
    assert "writing" in fallback_state.profiles
    assert fallback_state.profiles["writing"].name == "Custom Academic Writer"
    # Syntax warning and error_line are populated
    assert fallback_state.syntax_warning is not None
    assert "line" in fallback_state.syntax_warning.lower()
    assert fallback_state.error_line is not None
    assert fallback_state.error_line >= 4


_PRESERVATION_MARKER = "Keep every \\label, \\ref and \\cite key"
_GROUNDING_MARKER = "Never invent citation keys"
_UNTRUSTED_MARKER = "<untrusted_source_excerpt>"
_SEVERITY_MARKER = (
    "Severity: major = invalidates a claim or result; minor = weakens clarity or rigor; "
    "nit = cosmetic."
)


def test_editing_defaults_include_preservation_and_grounding_rules() -> None:
    checked = [
        p
        for p in DEFAULT_PROFILES.values()
        if p.role != "reviewer" and p.id not in {"explain_math", "verify_claim", "explain"}
    ]
    assert {p.id for p in checked} == {
        "writing",
        "synthesis",
        "proofreading",
        "tighten",
        "formal_tone",
        "fix_latex",
        "compile_autofix",
    }
    for prof in checked:
        assert _PRESERVATION_MARKER in prof.system_prompt, prof.id
        assert _GROUNDING_MARKER in prof.system_prompt, prof.id


def test_all_defaults_include_untrusted_excerpt_rule() -> None:
    for prof in DEFAULT_PROFILES.values():
        assert _UNTRUSTED_MARKER in prof.system_prompt, prof.id


def test_verify_claim_in_defaults_and_custom_yaml_with_it_still_loads(
    tmp_path: Path,
) -> None:
    assert "verify_claim" in DEFAULT_PROFILES
    prompts_path = tmp_path / "config" / "prompts.yaml"
    prompts_path.parent.mkdir(parents=True)
    prompts_path.write_text(
        "profiles:\n"
        "  writing:\n"
        "    name: W\n"
        "    role: writing\n"
        "    system_prompt: Write.\n"
        "  verify_claim:\n"
        "    name: Claim Verifier\n"
        "    role: quick_action\n"
        "    system_prompt: Verify.\n",
        encoding="utf-8",
    )
    state = PromptsManager(prompts_path=prompts_path).load_or_fallback()
    assert state.syntax_warning is None
    assert "verify_claim" in state.profiles


def test_existing_prompts_yaml_is_not_rewritten(tmp_path: Path) -> None:
    prompts_path = tmp_path / "config" / "prompts.yaml"
    prompts_path.parent.mkdir(parents=True)
    original = (
        "profiles:\n"
        "  writing:\n"
        "    name: Old Writer\n"
        "    role: writing\n"
        "    system_prompt: Old text.\n"
    ).encode("utf-8")
    prompts_path.write_bytes(original)

    manager = PromptsManager(prompts_path=prompts_path)
    manager.load_or_fallback()

    assert prompts_path.read_bytes() == original


def test_seeded_yaml_round_trips_new_defaults(tmp_path: Path) -> None:
    prompts_path = tmp_path / "config" / "prompts.yaml"
    PromptsManager(prompts_path=prompts_path)

    raw = yaml.safe_load(prompts_path.read_text(encoding="utf-8"))["profiles"]
    assert set(raw) == set(DEFAULT_PROFILES)
    for pid, prof in DEFAULT_PROFILES.items():
        assert raw[pid]["name"] == prof.name
        assert raw[pid]["role"] == prof.role
        assert raw[pid]["system_prompt"] == prof.system_prompt

    state = PromptsManager(prompts_path=prompts_path).load_or_fallback()
    assert state.profiles == DEFAULT_PROFILES


def test_reviewer_defaults_define_major_minor_nit() -> None:
    reviewers = [p for p in DEFAULT_PROFILES.values() if p.role == "reviewer"]
    assert {p.id for p in reviewers} == {
        "methodology_critic",
        "literature_auditor",
        "structure_editor",
    }
    for prof in reviewers:
        assert _SEVERITY_MARKER in prof.system_prompt, prof.id
