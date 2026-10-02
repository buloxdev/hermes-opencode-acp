import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin"))
from opencode_acp_client import (
    _format_incremental_prompt,
    _format_messages_as_prompt,
    _estimate_tokens,
    _parse_acp_usage,
    _resolve_catalog_model,
    _unresolved_model_error,
    _augment_prompt_error,
    OpenCodeACPClient,
)

# A slice of a real OpenCode 2.x session catalog (session/new configOptions).
V2_CATALOG = [
    "openai/chatgpt-image-latest",
    "openai/gpt-4.1",
    "openai/gpt-4o-mini",
    "openrouter/inclusionai/ling-3.1-flash",
    "openrouter/aion-labs/aion-2.0",
    "opencode/deepseek-v4-flash",
    "opencode/kimi-k2.6",
]
# A V1-era catalog (opencode 1.18.x advertised bare model names).
V1_CATALOG = [
    "deepseek-v4-flash",
    "mimo-v2.5-free",
    "ling-3.1-flash",
]


def test_incremental_includes_system_context():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "What tools do you have?"},
    ]
    tools = [{"function": {"name": "read_file", "description": "Read a file", "parameters": {}}}]
    result = _format_incremental_prompt(messages, tools=tools)
    assert "ACP agent" in result, f"Missing ACP role instruction in: {result[:200]}"
    assert "read_file" in result, f"Missing tool definition in: {result[:200]}"
    assert "Continue the conversation" in result, f"Missing continuation instruction"
    assert "What tools do you have?" in result, "Missing latest user text (tail)"
    print("PASS: incremental prompt includes system context + tail")


def test_incremental_without_tools():
    messages = [
        {"role": "user", "content": "Hello"},
    ]
    result = _format_incremental_prompt(messages)
    assert "ACP agent" in result, f"Missing ACP role instruction"
    assert "Continue the conversation" in result
    print("PASS: incremental prompt works without tools")


def test_incremental_empty_messages():
    assert _format_incremental_prompt([]) == "", "Empty messages should return ''"
    # No user message → full-format fallback path
    msgs = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "hi"}]
    assert _format_incremental_prompt(msgs) == "", "No user msg should return ''"
    print("PASS: incremental prompt fallback on empty/no-user")


def test_incremental_tool_roundtrip_tail():
    # Tool results arriving AFTER the latest user message (no new user
    # prompt yet) must be fed back so OpenCode sees what it called and
    # what the tool returned.
    messages = [
        {"role": "user", "content": "Add the curl call"},
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "content": '{"ok": true}', "tool_call_id": "1"},
    ]
    result = _format_incremental_prompt(messages)
    assert "Tool call: read_file" in result, "Tool roundtrip tail missing tool call info"
    assert '"ok": true' in result, "Tool result missing from tail"
    print("PASS: incremental prompt renders tool roundtrip tail")


def test_incremental_new_user_turn_after_roundtrip():
    # A NEW user message after the roundtrip: OpenCode's own session
    # history holds the prior call, so the tail is just the new request.
    messages = [
        {"role": "user", "content": "Add the curl call"},
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "content": '{"ok": true}', "tool_call_id": "1"},
        {"role": "user", "content": "now what"},
    ]
    result = _format_incremental_prompt(messages)
    assert "now what" in result, "Latest user message missing"
    print("PASS: incremental prompt sends just the new user request")


def test_full_prompt_unchanged_shape():
    messages = [{"role": "user", "content": "Hello"}]
    tools = [{"function": {"name": "ls", "description": "List", "parameters": {}}}]
    result = _format_messages_as_prompt(messages, tools=tools)
    assert "Conversation transcript" in result
    assert "ls" in result
    assert "Hello" in result
    print("PASS: full-conversation prompt still renders system + transcript")


def test_token_estimation():
    assert _estimate_tokens("") == 0
    assert _estimate_tokens("hello") >= 1
    long_text = "Hello world, this is a longer piece of text for testing." * 10
    tokens = _estimate_tokens(long_text)
    assert 50 < tokens < 500, f"Token count {tokens} seems wrong for {len(long_text)} chars"
    cjk = "你好世界，这是一个测试函数。" * 5
    cjk_tokens = _estimate_tokens(cjk)
    # CJK must not be crushed to ~1 token: ~1 token per CJK char.
    assert cjk_tokens >= len(cjk) // 2, f"CJK undercount {cjk_tokens} for {len(cjk)} chars"
    print(f"PASS: token estimation EN={tokens} CJK={cjk_tokens}")


def test_parse_acp_usage_real_shape():
    # The shape opencode acp (>=1.18.20) actually returns in the
    # session/prompt result — verified live with a real ACP session.
    raw = {
        "inputTokens": 51562,
        "outputTokens": 27,
        "totalTokens": 52613,
        "cachedReadTokens": 1024,
    }
    parsed = _parse_acp_usage(raw)
    assert parsed is not None
    assert parsed["prompt_tokens"] == 51562
    assert parsed["completion_tokens"] == 27
    assert parsed["cached_tokens"] == 1024
    # input + output + cached = the server's totalTokens (opencode's shape)
    assert parsed["prompt_tokens"] + parsed["completion_tokens"] + parsed["cached_tokens"] == parsed["total_tokens"] or parsed["total_tokens"] == 52613
    print(f"PASS: parse ACP usage real shape → {parsed}")


def test_parse_acp_usage_missing_or_zero():
    assert _parse_acp_usage(None) is None
    assert _parse_acp_usage({}) is None
    assert _parse_acp_usage({"inputTokens": 0, "outputTokens": 0}) is None
    # Missing totalTokens → computed as input + output
    parsed = _parse_acp_usage({"inputTokens": 100, "outputTokens": 5})
    assert parsed["total_tokens"] == 105
    # Non-dict (e.g. a bare string from an odd server) → None
    assert _parse_acp_usage("oops") is None
    print("PASS: parse ACP usage missing/zero/non-dict → None fallback")


# ── Model-id resolution: OpenCode V1 ↔ V2 catalogs ─────────────────────────


def test_resolve_exact_match():
    resolved, suggestions = _resolve_catalog_model("openai/gpt-4o-mini", V2_CATALOG)
    assert resolved == "openai/gpt-4o-mini", f"expected exact match, got {resolved}"
    assert suggestions == []
    print("PASS: resolve exact catalog match (V2 full provider/model id)")


def test_resolve_case_insensitive_match():
    resolved, _ = _resolve_catalog_model("OpenAI/GPT-4O-Mini", V2_CATALOG)
    assert resolved == "openai/gpt-4o-mini", f"expected catalog casing, got {resolved}"
    print("PASS: resolve case-insensitive match returns catalog spelling")


def test_resolve_bare_name_on_v2_catalog():
    # V1-style bare request against a V2 catalog → unique model-part match
    resolved, suggestions = _resolve_catalog_model("gpt-4o-mini", V2_CATALOG)
    assert resolved == "openai/gpt-4o-mini", f"expected full id, got {resolved}"
    assert suggestions == ["openai/gpt-4o-mini"]
    print("PASS: bare V1-style name resolves to full V2 provider/model id")


def test_resolve_prefixed_request_on_v1_catalog():
    # V2-style "opencode/<name>" request against a V1 bare-name catalog
    resolved, _ = _resolve_catalog_model("opencode/deepseek-v4-flash", V1_CATALOG)
    assert resolved == "deepseek-v4-flash", f"expected V1 bare name, got {resolved}"
    print("PASS: prefixed request resolves to V1 bare-name catalog entry")


def test_resolve_bare_name_prefers_opencode_hosted_tier():
    catalog = V2_CATALOG + ["openrouter/deepseek/deepseek-v4-flash"]
    resolved, suggestions = _resolve_catalog_model("deepseek-v4-flash", catalog)
    assert resolved == "opencode/deepseek-v4-flash", f"expected hosted tier, got {resolved}"
    assert len(suggestions) == 2
    print("PASS: ambiguous bare name prefers the opencode/ hosted tier")


def test_resolve_ambiguous_without_hosted_tier_is_unresolved():
    catalog = ["openai/foo-bar", "openrouter/x/foo-bar"]
    resolved, suggestions = _resolve_catalog_model("foo-bar", catalog)
    assert resolved is None, "ambiguous bare name must not resolve silently"
    assert sorted(suggestions) == ["openai/foo-bar", "openrouter/x/foo-bar"]
    print("PASS: ambiguous bare name lists both candidates instead of guessing")


def test_resolve_retired_model_id_is_unresolved():
    # opencode/mimo-v2.5-free was renamed upstream — must error, not fall back
    resolved, suggestions = _resolve_catalog_model("opencode/mimo-v2.5-free", V2_CATALOG)
    assert resolved is None, "retired id must not resolve"
    assert suggestions == []
    print("PASS: retired model id resolves to None (loud failure upstream)")


def test_resolve_empty_catalog_passes_request_through():
    # Server did not advertise configOptions — nothing to verify against
    resolved, _ = _resolve_catalog_model("anything/at-all", [])
    assert resolved == "anything/at-all"
    print("PASS: empty catalog passes the request through verbatim")


def test_resolve_empty_request_is_none():
    assert _resolve_catalog_model(None, V2_CATALOG) == (None, [])
    assert _resolve_catalog_model("", V2_CATALOG) == (None, [])
    print("PASS: empty request resolves to None")


# ── Error shaping for V2 model failures ───────────────────────────────────


def test_unresolved_model_error_names_request_and_fix():
    err = _unresolved_model_error("opencode/mimo-v2.5-free", [])
    text = str(err)
    assert "opencode/mimo-v2.5-free" in text
    assert "hermes model" in text
    assert "provider/model" in text
    print("PASS: unresolved-model error names the request and the fix")


def test_unresolved_model_error_lists_suggestions():
    err = _unresolved_model_error(
        "foo-bar", ["openai/foo-bar", "openrouter/x/foo-bar"]
    )
    assert "openai/foo-bar" in str(err)
    assert "openrouter/x/foo-bar" in str(err)
    print("PASS: unresolved-model error lists closest catalog matches")


def test_augment_prompt_error_rewrites_model_unavailable():
    raw = RuntimeError(
        "OpenCode ACP session/prompt failed: "
        "Internal error: Model unavailable: openai/gpt-4o-mini"
    )
    out = _augment_prompt_error(raw, "openai/gpt-4o-mini")
    assert out is not raw, "Model unavailable errors must be rewritten"
    text = str(out)
    assert "openai/gpt-4o-mini" in text
    assert "opencode auth login" in text
    assert "Internal error: Model unavailable" in text  # original kept
    print("PASS: 'Model unavailable' prompt error gains credential guidance")


def test_augment_prompt_error_passes_other_errors_through():
    raw = TimeoutError(
        "Timed out waiting for OpenCode ACP response to session/prompt."
    )
    out = _augment_prompt_error(raw, "openai/gpt-4o-mini")
    assert out is raw, "non-model errors must pass through untouched"
    print("PASS: non-model prompt errors pass through unchanged")


test_incremental_includes_system_context()
test_incremental_without_tools()
test_incremental_empty_messages()
test_incremental_tool_roundtrip_tail()
test_full_prompt_unchanged_shape()
test_token_estimation()
test_parse_acp_usage_real_shape()
test_parse_acp_usage_missing_or_zero()
test_resolve_exact_match()
test_resolve_case_insensitive_match()
test_resolve_bare_name_on_v2_catalog()
test_resolve_prefixed_request_on_v1_catalog()
test_resolve_bare_name_prefers_opencode_hosted_tier()
test_resolve_ambiguous_without_hosted_tier_is_unresolved()
test_resolve_retired_model_id_is_unresolved()
test_resolve_empty_catalog_passes_request_through()
test_resolve_empty_request_is_none()
test_unresolved_model_error_names_request_and_fix()
test_unresolved_model_error_lists_suggestions()
test_augment_prompt_error_rewrites_model_unavailable()
test_augment_prompt_error_passes_other_errors_through()
print("\nAll tests passed!")