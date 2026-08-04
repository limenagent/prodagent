from __future__ import annotations

import pytest

from prodagent.llm.structured_output import extract_json_object


class TestExtractJsonObject:
    def test_pure_json_object(self) -> None:
        text = '{"name": "Alice", "age": 30}'
        assert extract_json_object(text) == text

    def test_pure_json_array(self) -> None:
        text = "[1, 2, 3]"
        assert extract_json_object(text) == text

    def test_fenced_json_object(self) -> None:
        text = '```json\n{"name": "Bob"}\n```'
        assert extract_json_object(text) == '{"name": "Bob"}'

    def test_fenced_json_without_language_tag(self) -> None:
        text = '```\n{"k": "v"}\n```'
        assert extract_json_object(text) == '{"k": "v"}'

    def test_json_with_leading_prose(self) -> None:
        text = 'Here is the answer:\n{"answer": 42}\nThat is all.'
        assert extract_json_object(text) == '{"answer": 42}'

    def test_json_with_trailing_prose(self) -> None:
        text = '{"answer": 42}\nHope this helps!'
        assert extract_json_object(text) == '{"answer": 42}'

    def test_nested_objects(self) -> None:
        text = '{"outer": {"inner": {"deep": true}}}'
        assert extract_json_object(text) == text

    def test_nested_arrays_in_object(self) -> None:
        text = '{"items": [1, 2, {"x": 3}]}'
        assert extract_json_object(text) == text

    def test_braces_inside_string_literals_dont_confuse_parser(self) -> None:
        text = '{"k": "val}{val2}{"}'
        assert extract_json_object(text) == text

    def test_array_with_object_first_wins(self) -> None:
        text = '[{"k": 1}]'
        assert extract_json_object(text) == '[{"k": 1}]'

    def test_object_before_array_wins(self) -> None:
        text = '{"a": 1} and then [1, 2]'
        assert extract_json_object(text) == '{"a": 1}'

    def test_no_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="No JSON object or array"):
            extract_json_object("just plain text")

    def test_unmatched_brace_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unmatched"):
            extract_json_object('{"k": "value"')

    def test_escaped_quotes_in_strings(self) -> None:
        text = r'{"k": "she said \"hi\""}'
        assert extract_json_object(text) == text

    def test_strips_whitespace_around_fence(self) -> None:
        text = '  ```json\n{"x": 1}\n```  '
        assert extract_json_object(text) == '{"x": 1}'

    def test_skips_python_dict_repr_in_prose(self) -> None:
        text = (
            "Let me analyze the transcript. The agent called "
            "{'service': 'payment-service', 'metric': 'memory_rss'} "
            "and got a result.\n\n"
            '```json\n{"name": "rollback-skill", "procedure": "step 1"}\n```'
        )
        assert extract_json_object(text) == '{"name": "rollback-skill", "procedure": "step 1"}'

    def test_skips_multiple_non_json_braces(self) -> None:
        text = "First {a: 1} then {'b': 2} then {c: 'x'} finally {\"real\": true}"
        assert extract_json_object(text) == '{"real": true}'
