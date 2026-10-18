""" Tests for inqtrix.json_helpers — JSON parsing from LLM output."""

from inqtrix.json_helpers import parse_json_string_list, parse_json_object


class TestParseJsonStringList:

    def test_valid_json_array(self):
        assert parse_json_string_list('["a", "b", "c"]', fallback=["x"]) == ["a", "b", "c"]

    def test_code_fenced_json(self):
        text = '```json\n["query1", "query2"]\n```'
        assert parse_json_string_list(text, fallback=["x"]) == ["query1", "query2"]

    def test_max_items(self):
        result = parse_json_string_list('["a", "b", "c", "d"]', fallback=["x"], max_items=2)
        assert result == ["a", "b"]

    def test_fallback_on_invalid_json(self):
        assert parse_json_string_list("not json", fallback=["fallback"]) == ["fallback"]

    def test_fallback_on_empty_string(self):
        assert parse_json_string_list("", fallback=["fallback"]) == ["fallback"]

    def test_fallback_on_none(self):
        assert parse_json_string_list(None, fallback=["fallback"]) == ["fallback"]

    def test_json_embedded_in_text(self):
        text = 'Here are the queries: ["q1", "q2"] and more text'
        assert parse_json_string_list(text, fallback=["x"]) == ["q1", "q2"]

    def test_empty_array_returns_fallback(self):
        assert parse_json_string_list("[]", fallback=["fallback"]) == ["fallback"]

    def test_filters_empty_strings(self):
        result = parse_json_string_list('["a", "", "b"]', fallback=["x"])
        assert "" not in result
        assert "a" in result
        assert "b" in result

    def test_non_string_items_converted(self):
        result = parse_json_string_list('[1, 2, 3]', fallback=["x"])
        assert result == ["1", "2", "3"]


class TestParseJsonObject:

    def test_valid_object(self):
        assert parse_json_object('{"a": 1}', fallback={"x": 0}) == {"a": 1}

    def test_code_fenced_object(self):
        text = '```json\n{"k": "v"}\n```'
        assert parse_json_object(text, fallback={}) == {"k": "v"}

    def test_embedded_object(self):
        text = "prefix {\"foo\": \"bar\"} suffix"
        assert parse_json_object(text, fallback={}) == {"foo": "bar"}

    def test_invalid_returns_fallback(self):
        assert parse_json_object("not json", fallback={"ok": True}) == {"ok": True}
