import copy
import pytest

from app.integrations.constants import SUPPORTED_HOSTS
from app.integrations.host_capability_matrix import (
    CONFIG_CAPABILITIES,
    CONTEXT_CAPABILITIES,
    config_matrix,
    context_matrix,
    inherited_live_passes,
    load_evidence,
)


def test_two_axis_matrix_covers_every_host_and_capability():
    evidence = load_evidence()
    context = context_matrix(evidence)
    config = config_matrix(evidence)
    assert set(context) == set(SUPPORTED_HOSTS)
    assert set(config) == set(SUPPORTED_HOSTS)
    for host in SUPPORTED_HOSTS:
        assert set(context[host]) == set(CONTEXT_CAPABILITIES)
        assert set(config[host]) == set(CONFIG_CAPABILITIES)
        for cell in context[host].values():
            assert "grade" in cell and "evidence" in cell
            assert cell["verified"] in {True, False}


def test_new_hosts_do_not_inherit_live_pass_conclusions():
    assert inherited_live_passes() == []


def test_inherited_live_passes_detects_copied_donor_evidence():
    evidence = load_evidence()

    # Mutating opencode to copy a donor host's context matrix must fail the invariant
    mutated = copy.deepcopy(evidence)
    mutated["context_handoff"]["opencode"] = copy.deepcopy(mutated["context_handoff"]["codex"])
    problems = inherited_live_passes(mutated)
    assert any("identical to another host" in p for p in problems)

    # Mutating opencode live_host cell to omit host_version must fail
    mutated2 = copy.deepcopy(evidence)
    mutated2["context_handoff"]["opencode"]["confirm_then_continue"]["host_version"] = None
    problems2 = inherited_live_passes(mutated2)
    assert any("requires its own host_version" in p for p in problems2)

    # Mutating opencode live_host cell to copy donor evidence text must fail
    mutated3 = copy.deepcopy(evidence)
    mutated3["context_handoff"]["opencode"]["confirm_then_continue"]["evidence"] = evidence["context_handoff"]["codex"]["confirm_then_continue"]["evidence"]
    problems3 = inherited_live_passes(mutated3)
    assert any("cannot copy another host's evidence" in p for p in problems3)


def test_templates_fail_explicitly_when_packaged_assets_missing(monkeypatch, tmp_path):
    from app.integrations import templates

    missing_path = tmp_path / "nonexistent.ts"
    monkeypatch.setattr(templates, "OMP_EXTENSION_PATH", missing_path)
    monkeypatch.setattr(templates, "OPENCODE_PLUGIN_PATH", missing_path)

    with pytest.raises(FileNotFoundError):
        templates.render_hooks_config("omp")
    with pytest.raises(FileNotFoundError):
        templates.render_hooks_config("opencode")

    empty_skills_dir = tmp_path / "empty_skills"
    empty_skills_dir.mkdir()
    monkeypatch.setattr(templates, "SKILLS_DIR", empty_skills_dir)
    for host in SUPPORTED_HOSTS:
        with pytest.raises(FileNotFoundError):
            templates.render_skill_or_command(host)
