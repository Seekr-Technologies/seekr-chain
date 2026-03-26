#!/usr/bin/env python3

import re


def _parse_quantifier(quantifier) -> tuple[int, int | float]:
    if quantifier == "*":
        return 0, float("inf")
    elif quantifier == "+":
        return 1, float("inf")
    elif quantifier == "?":
        return 0, 1
    else:
        quantifier = quantifier[1:-1]  # strip '{' and '}'
        parts = quantifier.split(",")
        if len(parts) == 1:
            min_count = max_count = int(parts[0])
        else:
            min_count = int(parts[0])
            max_count = int(parts[1]) if parts[1] else float("inf")
        return min_count, max_count


def _get_pattern_components(pattern):
    if isinstance(pattern, tuple):
        return pattern
    return pattern, "{1}"


def _get_depth(context):
    """
    Get the 'depth' of a context.

    Here, we define depth as the sum of the number of actual and pattern lines consumed.
    """
    return context[0] + context[1]


def _try_match(actual, patterns, actual_index, pattern_index, level):
    if pattern_index == len(patterns):
        success = actual_index == len(actual)
        if success:
            return True, None
        return False, (actual_index, pattern_index, "Ran out of patterns before matching all lines")

    if actual_index >= len(actual):
        # If we have one pattern left, and its (...,"*"), that's ok, as this is "match 0 or more times"
        if (
            pattern_index == len(patterns) - 1
            and isinstance(patterns[pattern_index], tuple)
            and patterns[pattern_index][1] == "*"
        ):
            return True, None
        return False, (
            actual_index,
            pattern_index,
            f"Expected pattern {patterns[pattern_index]!r} but ran out of lines",
        )

    # Parse out the regex and quantifier string, and extract as min/max count ints (or inf)
    regex, quantifier = _get_pattern_components(patterns[pattern_index])
    min_count, max_count = _parse_quantifier(quantifier)

    # Match as many lines as possible with this regex. Keep track of how many lines we match
    matched = 0
    first_fail_msg = None
    while actual_index + matched < len(actual) and matched < max_count:
        line = actual[actual_index + matched]
        try:
            if re.fullmatch(regex, line):
                matched += 1
            else:
                first_fail_msg = f"Pattern {regex!r} does not match line {line!r}"
                break
        except Exception as e:
            raise ValueError(f"Error matching!\n  Patterh: {regex}\n  Line: {line}.\n See above exception") from e

    # track best (Deepest) failure context seen
    counts = reversed(range(min_count, matched + 1))
    deepest_failure_ctx = (
        actual_index,
        pattern_index,
        first_fail_msg or f"Pattern {regex!r} did not match enough lines",
    )

    # RECURSIVELY MATCH
    # Move to the next pattern, and skip as many lines as we matched (greedy).
    # If we fail, pop one line from the current count and continue. This gracefully handles matching
    # patterns like `.*dc` to `abddc`, where `.*` should match `abd` (not just `ab`)
    for count in counts:
        success, fail_ctx = _try_match(actual, patterns, actual_index + count, pattern_index + 1, level + 1)
        if success:
            return True, None
        else:
            # Retain the deepest failure ctx seen so far
            if _get_depth(fail_ctx) > _get_depth(deepest_failure_ctx):
                deepest_failure_ctx = fail_ctx

    return False, deepest_failure_ctx


def assert_patterns_match(actual, patterns):
    success, fail_ctx = _try_match(actual, patterns, 0, 0, level=0)
    if not success:
        actual_index, pattern_index, reason = fail_ctx

        actual_str = "\n".join([f"{' -> ' if i == actual_index else '    '}{line}" for i, line in enumerate(actual)])
        pattern_str = "\n".join([f"{' -> ' if i == pattern_index else '    '}{p}" for i, p in enumerate(patterns)])

        raise AssertionError(
            f"Patterns to not match.\nReason: {reason}\n\nActual:\n{actual_str}\n\nPatterns:\n{pattern_str}\n"
        )


def assert_nested_match(actual, expected):
    """
    General utility for asserting nested matches. Works with various datatypes.

    Strings are always matched as patterns
    """
    assert type(actual) is type(expected)

    if isinstance(actual, (int, bool)):
        # Direct comparison for integers and floats
        assert actual == expected
    elif isinstance(actual, str):
        if not re.match(expected, actual):
            raise AssertionError(f"Regex does not match.\n  regex: {expected}\n  str: {actual}")
    elif isinstance(actual, list):
        # For a list of only strings, we toss it over to patterns_match.
        if all([isinstance(item, str) for item in actual]):
            assert_patterns_match(actual, expected)

        # Otherwise, recurse
        else:
            assert len(actual) == len(expected)
            for a, b in zip(actual, expected):
                assert_nested_match(a, b)

        pass
    elif isinstance(actual, dict):
        if set(actual.keys()) != set(expected.keys()):
            actual_keys = set(actual.keys())
            expected_keys = set(expected.keys())
            missing_keys = expected_keys - actual_keys
            extra_keys = actual_keys - expected_keys

            msg_lines = ["Dict key mismatch:"]
            if missing_keys:
                msg_lines.append(f"  Missing in actual: {sorted(missing_keys)}")
            if extra_keys:
                msg_lines.append(f"  Unexpected in actual: {sorted(extra_keys)}")
            raise AssertionError("\n".join(msg_lines))

        for key in actual.keys():
            assert_nested_match(actual[key], expected[key])
    else:
        raise NotImplementedError(f"Nested match not implemented for type: {type(actual)}")
