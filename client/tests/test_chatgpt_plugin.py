"""The ChatGPT plugin (chatgpt-plugin/): the package for OpenAI's plugin directory, shared by
ChatGPT and Codex. The directory refuses lifecycle hooks and app references and needs one remote
HTTPS MCP server, the four listing URLs and exactly five positive and three negative review cases
(developers.openai.com/plugins/deploy/submission, read 2026-11-23)."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from agentygg.render import remote_plugin_skill_md

PLUGIN = Path(__file__).resolve().parents[1] / "chatgpt-plugin"


@pytest.fixture
def manifest() -> dict:
    if not PLUGIN.is_dir():
        pytest.skip("the plugin is not next to this client")
    return json.loads((PLUGIN / "plugin.json").read_text())


def test_the_skills_match_the_client(manifest):
    assert (PLUGIN / "skills" / "agentygg" / "SKILL.md").read_text() == remote_plugin_skill_md()
    trouble = resources.files("agentygg").joinpath("skill/TROUBLESHOOTING.md").read_text()
    assert (PLUGIN / "skills" / "agentygg" / "TROUBLESHOOTING.md").read_text() == trouble
    assert "ygg listen" not in remote_plugin_skill_md()
    assert "ygg relay" not in remote_plugin_skill_md()


def test_the_package_is_one_the_directory_accepts(manifest):
    assert not (PLUGIN / "hooks").exists() and not (PLUGIN / ".app.json").exists()
    assert not (PLUGIN / ".codex-plugin").exists()  # the root manifest alone, no overlay
    openai = manifest["extensions"]["com.openai"]
    assert "hooks" not in openai and "apps" not in openai
    servers = json.loads((PLUGIN / "mcp.json").read_text())["mcpServers"]
    assert list(servers) == ["agentygg"]
    server = servers["agentygg"]
    assert server["type"] == "streamable-http"
    assert server["url"] == "https://api.agentygg.com/v1/agent/mcp" and not server.get("headers")
    assert server["extensions"]["com.openai"]["auth"] == {"type": "oauth", "client": {"mode": "dcr"}}


def test_the_listing_fits_the_submission_limits(manifest):
    ui = manifest["extensions"]["com.openai"]["interface"]
    assert len(ui["displayName"]) <= 30 and len(ui["shortDescription"]) <= 30
    assert len(ui["longDescription"]) <= 4000
    for key in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
        assert ui[key].startswith("https://"), key
    assert len(ui["defaultPrompt"]) <= 3 and all(len(p) <= 128 for p in ui["defaultPrompt"])
    assert not any("@" in p for p in ui["defaultPrompt"])
    for key in ("logo", "composerIcon"):
        assert ui[key].startswith("./") and (PLUGIN / ui[key]).is_file(), key
    onboarding = manifest["extensions"]["com.openai"]["onboardingSkill"]
    assert (PLUGIN / onboarding).is_file()
    assert manifest["name"] == "agentygg"


def test_the_review_cases_are_complete(manifest):
    cases = manifest["extensions"]["com.openai"]["review"]["test_cases"]
    assert len(cases["positive"]) == 5 and len(cases["negative"]) == 3
    tools = {"ygg_inbox", "ygg_send", "ygg_conversation", "ygg_me", "ygg_directory", "ygg_tasks",
             "ygg_upload", "ygg_download"}
    for case in cases["positive"]:
        assert case["description"] and case["prompt"] and case["expected_behavior"]
        named = {t.strip() for t in case["tools_triggered"].split(",")}
        assert named and named <= tools, named
    for case in cases["negative"]:
        assert case["description"] and case["prompt"]
    # Reviewer credentials never go in the package (the dashboard form holds them).
    review = manifest["extensions"]["com.openai"]["review"]
    assert "test_credentials" not in review and "reviewer_instructions" not in review
