#!/usr/bin/env python3

import pytest

from seekr_chain._testing import assert_nested_match, assert_patterns_match


class TestPatternMatch:
    def test_basic(self):
        # Test 1: exact matches, should pass
        assert_patterns_match(
            ["hello world", "my name is andy"],
            ["hello world", "my name is andy"],
        )

    def test_regex(self):
        # Test 2: simple regex, should pass
        assert_patterns_match(
            ["hello world", "my name is bob"],
            ["hello world", "my name is .*"],
        )

    def test_static_quantifier(self):
        # Test 3: simple regex with quantifier, should pass
        assert_patterns_match(
            ["hello world", "hello world", "hello world", "my name is bob"], [("hello world", "{3}"), "my name is .*"]
        )

    def test_range_quantifier(self):
        # Test 4: simple regex with range quantifier, should pass
        assert_patterns_match(
            ["hello world", "hello world", "my name is bob"], [("hello world", "{1,3}"), "my name is .*"]
        )

    def test_greedy_giveback(self):
        assert_patterns_match(
            [
                "a",
                "b",
                "c",
            ],
            [
                (".*", "*"),
                "c",
            ],
        )

    def test_greedy_fail(self):
        with pytest.raises(AssertionError):
            assert_patterns_match(
                [
                    "a",
                    "b",
                    "c",
                ],
                [
                    (".*", "*"),
                    "d",
                ],
            )

    def test_patterns_used_once(self):
        assert_patterns_match(
            [
                "a",
                "b",
                "c",
            ],
            [
                ".*",
                "b",
                "c",
            ],
        )

    def test_exact_match_fail(self):
        # Test 5: exact match should fail
        with pytest.raises(AssertionError):
            assert_patterns_match(
                [
                    "hello world",
                    "my name is bob",
                ],
                [
                    "hello world",
                    "my name is andy",
                ],
            )

    def test_regex_fail(self):
        # Test 6: simple regex should fail
        with pytest.raises(AssertionError):
            assert_patterns_match(
                [
                    "hello world",
                    "goodbye world",
                ],
                [
                    "hello world",
                    "my name is .*",
                ],
            )

    def test_regex_quantifier_fail(self):
        # Test 7: simple regex with quantifier, should fail
        with pytest.raises(AssertionError):
            assert_patterns_match(
                [
                    "hello world",
                    "hello world",
                    "hello world",
                    "my name is bob",
                ],
                [
                    ("hello world", "{2}"),
                    "my name is .*",
                ],
            )

    def test_quantifier_exact_match(self):
        # Test 8: exact matches with quantifier, should pass
        assert_patterns_match(["hello world", "hello world", "hello world"], [("hello world", "{3}")])

    def test_star_greedy(self):
        # Greedy: .* matches as many as possible
        assert_patterns_match(
            [
                "a",
                "b",
                "c",
            ],
            [
                (".*", "*"),
                "c",
            ],
        )

    def test_plus_greedy(self):
        # Greedy: .+ matches as many as possible
        assert_patterns_match(["x", "x", "end"], [("x", "+"), "end"])

    def test_question_greedy(self):
        # Greedy: optional match
        assert_patterns_match(["maybe", "yes"], [("maybe", "?"), "yes"])
        # Also succeeds with no match
        assert_patterns_match(["yes"], [("maybe", "?"), "yes"])

    def test_exact_count(self):
        assert_patterns_match(
            ["x", "x"],
            [
                ("x", "{2}"),
            ],
        )
        with pytest.raises(AssertionError):
            assert_patterns_match(
                ["x", "x", "x"],
                [
                    ("x", "{2}"),
                ],
            )

    def test_range_count_greedy(self):
        # Greedy: should consume 3 if possible
        assert_patterns_match(["x", "x", "x", "y"], [("x", "{1,3}"), "y"])

    def test_fail_match_extra_lines(self):
        # Fails because there's an extra unmatched line
        with pytest.raises(AssertionError):
            assert_patterns_match(["a", "b", "c"], [("a", "{1}")])


class TestNestedPatternMatch:
    def test_basic(self):
        # Test 1: exact matches, should pass
        assert_nested_match(
            actual={
                "int": 42,
                "bool": True,
                "str": "hello-there-abcd-world",
                "list": ["hello", "world"],
                "dict": {
                    "a": 42,
                    "b": ["nested", "hello", "world"],
                },
            },
            expected={
                "int": 42,
                "bool": True,
                "str": "hello-there-.*-world",
                "list": ["hello", ".*"],
                "dict": {
                    "a": 42,
                    "b": ["nested", "he.*", "world"],
                },
            },
        )
