from __future__ import annotations

from prodagent.core.text import _CJK, cjk_char_count, tokenize_cjk


class TestCJKBlockCoverage:
    def test_main_block_characters_match(self):
        main_block = "中文汉字代码北京上海"
        for c in main_block:
            assert _CJK.match(c), f"U+{ord(c):04X} ({c}) must match the CJK class"

    def test_extension_a_characters_match(self):
        ext_a = "㐀㐁㐂㐃㐄"
        for c in ext_a:
            assert _CJK.match(c), f"U+{ord(c):04X} ({c}) must match the CJK class"

    def test_compatibility_ideographs_match(self):
        compat = "豈更車賈滑"
        for c in compat:
            assert _CJK.match(c), f"U+{ord(c):04X} ({c}) must match the CJK class"

    def test_ascii_does_not_match(self):
        for c in "abcdefABCDEF0123456789":
            assert not _CJK.match(c), f"ASCII {c!r} must not match the CJK class"

    def test_kana_not_matched(self):
        for c in "あいうえおアイウエオ":
            assert not _CJK.match(c), (
                f"U+{ord(c):04X} ({c}) — kana is intentionally not CJK-class; "
                "if you need kana tokenisation, add a separate range."
            )


class TestCjkCharCount:
    def test_counts_main_block(self):
        assert cjk_char_count("中文汉字") == 4

    def test_counts_mixed_text(self):
        assert cjk_char_count("中文汉字 code!") == 4

    def test_zero_for_ascii_only(self):
        assert cjk_char_count("plain ascii text") == 0

    def test_zero_for_empty(self):
        assert cjk_char_count("") == 0


class TestTokenizeCjk:
    def test_chinese_produces_ngrams(self):
        tokens = tokenize_cjk("中文")
        assert "中文" in tokens

    def test_mixed_chinese_english(self):
        tokens = tokenize_cjk("read 中文 please")
        assert "read" in tokens
        assert "please" in tokens
        assert "中文" in tokens

    def test_empty_string(self):
        assert tokenize_cjk("") == []

    def test_single_cjk_char_dropped(self):
        tokens = tokenize_cjk("字")
        assert tokens == []

    def test_long_chinese_run_produces_overlapping_ngrams(self):
        tokens = tokenize_cjk("中文字")
        assert "中文" in tokens
        assert "文字" in tokens
        assert "中文字" in tokens
