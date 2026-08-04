from prodagent.cognition.context.budget import TokenCounter


class TestTokenCounterQuirks:
    def test_count_empty_string_returns_zero(self):
        c = TokenCounter()
        assert c.count("") == 0

    def test_count_non_empty_is_positive(self):
        c = TokenCounter()
        assert c.count("hello world") >= 1

    def test_count_message_list_content_sums_blocks(self):
        c = TokenCounter()
        msg = {"role": "user", "content": ["block one", "block two"]}
        expected = c.count("block one") + c.count("block two")
        assert c.count_message(msg) == expected

    def test_count_message_dict_blocks_use_text_field(self):
        c = TokenCounter()
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
            ],
        }
        expected = c.count("hello") + c.count("world")
        assert c.count_message(msg) == expected

    def test_count_message_string_content(self):
        c = TokenCounter()
        msg = {"role": "user", "content": "hello"}
        assert c.count_message(msg) == c.count("hello")

    def test_count_message_does_not_count_role_overhead(self):
        c = TokenCounter()
        same_content = "hello world"
        for role in ("user", "assistant", "system", "tool"):
            msg = {"role": role, "content": same_content}
            assert c.count_message(msg) == c.count(same_content)

    def test_count_message_missing_content_defaults_to_empty(self):
        c = TokenCounter()
        assert c.count_message({"role": "user"}) == 0


class TestTokenCounterEstimator:
    def test_ascii_estimate_roughly_one_token_per_four_chars(self):
        c = TokenCounter()
        count = c.count("a" * 20)
        assert count == 5, f"20 ASCII chars should be ~5 tokens, got {count}"

    def test_cjk_estimate_is_denser_than_ascii(self):
        c = TokenCounter()
        ascii_count = c.count("a" * 10)
        cjk_count = c.count("字" * 10)
        assert cjk_count > ascii_count, (
            f"CJK should be denser: 10 汉字 = {cjk_count} tokens, 10 ASCII = {ascii_count} tokens"
        )

    def test_mixed_text_sums_both_components(self):
        c = TokenCounter()
        count = c.count("abcdefgh字字字")
        assert count == 4, f"expected 4 tokens (2 ASCII + 2 CJK), got {count}"

    def test_no_tiktoken_import(self):

        c = TokenCounter()
        assert c.count("hello") >= 1
        assert not hasattr(c, "_enc")
        assert not hasattr(c, "_tiktoken_failed")
        assert not hasattr(c, "_encoding")
