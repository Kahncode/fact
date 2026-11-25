from fact.core.types import limit_text


def test_limit_text_short_unchanged() -> None:
    assert limit_text("hello", 10) == "hello"


def test_limit_text_cuts_to_bytes() -> None:
    assert limit_text("abcdef", 3) == "abc"


def test_limit_text_cuts_on_character_boundary() -> None:
    s = "aé€😀"  # 1, 2, 3 and 4 bytes
    assert limit_text(s, 2) == "a"
    assert limit_text(s, 3) == "aé"
    assert limit_text(s, 5) == "aé"
    assert limit_text(s, 6) == "aé€"
    assert limit_text(s, 9) == "aé€"
    assert limit_text(s, 10) == s
    for n in range(len(s.encode()) + 1):
        assert len(limit_text(s, n).encode()) <= n


def test_limit_text_non_positive() -> None:
    assert limit_text("abc", 0) == ""
    assert limit_text("abc", -1) == ""
