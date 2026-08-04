from prodagent.resilience.observability.scrubber import (
    _REDACTED,
    DefaultScrubber,
    PassthroughScrubber,
)


def test_scrubber_redacts_sensitive_keys_case_insensitive():
    scrubber = DefaultScrubber()
    payload = {
        "Password": "secret123",
        "API_TOKEN": "sk-abc123",
        "secret": "hidden",
        "safe_key": "visible",
    }
    result = scrubber.scrub(payload)
    assert result["Password"] == _REDACTED
    assert result["API_TOKEN"] == _REDACTED
    assert result["secret"] == _REDACTED
    assert result["safe_key"] == "visible"


def test_scrubber_redacts_openai_key_patterns():
    scrubber = DefaultScrubber()
    payload = {
        "key": "sk-verylongkeyherethatisatleast20chars",
        "secret": "sk-proj-abc123def456",
    }
    result = scrubber.scrub(payload)
    assert result["key"] == _REDACTED
    assert result["secret"] == _REDACTED


def test_scrubber_redacts_anthropic_key_patterns():
    scrubber = DefaultScrubber()
    payload = {"key": "sk-ant-api03-abc123-def456789012"}
    result = scrubber.scrub(payload)
    assert result["key"] == _REDACTED


def test_scrubber_redacts_github_pat():
    scrubber = DefaultScrubber()
    payload = {"token": "ghp_1234567890abcdef1234567890abcdef12345678"}
    result = scrubber.scrub(payload)
    assert result["token"] == _REDACTED


def test_scrubber_redacts_github_actions_token():
    scrubber = DefaultScrubber()
    payload = {"token": "ghs_1234567890abcdef1234567890abcdef12345678"}
    result = scrubber.scrub(payload)
    assert result["token"] == _REDACTED


def test_scrubber_redacts_aws_access_key_id():
    scrubber = DefaultScrubber()
    payload = {"key": "AKIAIOSFODNN7EXAMPLE"}
    result = scrubber.scrub(payload)
    assert result["key"] == _REDACTED


def test_scrubber_redacts_ssn():
    scrubber = DefaultScrubber()
    payload = {"ssn": "123-45-6789"}
    result = scrubber.scrub(payload)
    assert result["ssn"] == _REDACTED


def test_scrubber_redacts_visa_and_mastercard():
    scrubber = DefaultScrubber()
    tests = [
        "4111111111111111",
        "5454545454545454",
    ]
    for card in tests:
        payload = {"card": card}
        result = scrubber.scrub(payload)
        assert result["card"] == _REDACTED


def test_scrubber_handles_nested_dicts():
    scrubber = DefaultScrubber()
    payload = {
        "user": {
            "name": "Alice",
            "credentials": {"password": "pw123"},
            "metadata": {"api_key": "sk-abc123"},
        }
    }
    result = scrubber.scrub(payload)
    assert result["user"]["name"] == "Alice"
    assert result["user"]["credentials"]["password"] == _REDACTED
    assert result["user"]["metadata"]["api_key"] == _REDACTED


def test_scrubber_handles_lists():
    scrubber = DefaultScrubber()
    payload = {
        "items": [
            {"name": "item1", "token": "sk-abc"},
            {"name": "item2", "secret": "hidden"},
            "safe_value",
        ]
    }
    result = scrubber.scrub(payload)
    assert result["items"][0]["name"] == "item1"
    assert result["items"][0]["token"] == _REDACTED
    assert result["items"][1]["name"] == "item2"
    assert result["items"][1]["secret"] == _REDACTED
    assert result["items"][2] == "safe_value"


def test_scrubber_redacts_dict_in_list_by_key():
    scrubber = DefaultScrubber()
    payload = {"items": [{"password": "x", "name": "alice"}]}
    result = scrubber.scrub(payload)
    assert result["items"][0]["password"] == _REDACTED
    assert result["items"][0]["name"] == "alice"


def test_scrubber_redacts_nested_dict_in_list_in_dict():
    scrubber = DefaultScrubber()
    payload = {
        "outer": {
            "records": [
                {"user": "a", "api_key": "short"},
                {"user": "b", "token": "t"},
            ]
        }
    }
    result = scrubber.scrub(payload)
    assert result["outer"]["records"][0]["user"] == "a"
    assert result["outer"]["records"][0]["api_key"] == _REDACTED
    assert result["outer"]["records"][1]["user"] == "b"
    assert result["outer"]["records"][1]["token"] == _REDACTED


def test_scrubber_extra_keys():
    scrubber = DefaultScrubber(extra_keys=frozenset({"patient_id", "dob", "mrn"}))
    payload = {
        "password": "pw123",
        "patient_id": "PAT-123",
        "dob": "1980-01-01",
        "name": "Alice",
    }
    result = scrubber.scrub(payload)
    assert result["password"] == _REDACTED
    assert result["patient_id"] == _REDACTED
    assert result["dob"] == _REDACTED
    assert result["name"] == "Alice"


def test_scrubber_preserves_non_sensitive_data():
    scrubber = DefaultScrubber()
    payload = {
        "name": "Alice",
        "age": 30,
        "email": "<EMAIL_ADDRESS>",
        "message": "Hello world",
    }
    result = scrubber.scrub(payload)
    assert result == payload


def test_scrubber_handles_empty_structures():
    scrubber = DefaultScrubber()
    assert scrubber.scrub({}) == {}
    assert scrubber.scrub({"key": "val"}) == {"key": "val"}
    assert scrubber.scrub({"list": []}) == {"list": []}
    assert scrubber.scrub({"dict": {}}) == {"dict": {}}


def test_passthrough_scrubber():
    scrubber = PassthroughScrubber()
    payload = {"password": "secret", "token": "sk-abc123"}
    result = scrubber.scrub(payload)
    assert result == payload


def test_scrubber_all_sensitive_keys():
    scrubber = DefaultScrubber()
    from prodagent.resilience.observability.scrubber import _SENSITIVE_KEYS

    for key in _SENSITIVE_KEYS:
        result = scrubber.scrub({key: "value"})
        assert result[key] == _REDACTED, f"Key {key} not redacted"
