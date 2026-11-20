from catalyst_agents.llm.client import ChatClient


def _extra(base_url: str) -> dict:
    client = ChatClient(model="m", api_key="k", base_url=base_url, extra_body={"thinking": {"type": "disabled"}})
    return client.bind(extra_body={"thinking": {"type": "disabled"}, "keep": 1}).request([{"role": "user", "content": "x"}])["extra_body"]


def test_thinking_switch_is_sent_to_deepseek():
    assert _extra("https://api.deepseek.com/v1")["thinking"] == {"type": "disabled"}


def test_thinking_switch_is_dropped_for_deepseek_lookalike_host():
    assert "thinking" not in _extra("https://api.deepseek.com.evil.example/v1")


def test_thinking_switch_is_dropped_for_openai_but_other_fields_stay():
    extra = _extra("https://api.openai.com/v1")
    assert "thinking" not in extra
    assert extra["keep"] == 1
