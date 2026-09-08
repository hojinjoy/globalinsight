"""Tests for globalinsight.clean: HTML -> plain text, and token estimation."""

from globalinsight import clean


def test_strips_script_and_style_content():
    raw = (
        "<html><head><style>.a{color:red}</style></head>"
        "<body><script>alert('hi');</script><p>hello world</p></body></html>"
    )
    text = clean.html_to_text(raw)
    assert "hello world" in text
    assert "alert" not in text
    assert "color" not in text
    assert "<script" not in text
    assert "<style" not in text


def test_strips_all_tags():
    raw = "<div class='x'><p>one</p><p>two</p></div>"
    text = clean.html_to_text(raw)
    assert "<" not in text
    assert ">" not in text
    assert "one" in text
    assert "two" in text


def test_unescape_happens_after_tag_stripping():
    """An escaped '<div>' in source HTML must survive as literal text, not be
    mistaken for a real tag once unescaped - so unescaping must happen AFTER
    tag-stripping, not before."""
    raw = "<p>See &lt;div&gt; for an example</p>"
    text = clean.html_to_text(raw)
    assert "<div>" in text


def test_entity_unescaping_common_entities():
    raw = "<p>Tom &amp; Jerry&#39;s caf&eacute; &mdash; 100&nbsp;%</p>"
    text = clean.html_to_text(raw)
    assert "Tom & Jerry's caf" in text
    assert "&amp;" not in text
    assert "&#39;" not in text


def test_nbsp_replaced_with_regular_space():
    raw = "<p>Revenue\xa0increased</p>"
    text = clean.html_to_text(raw)
    assert "\xa0" not in text
    assert "Revenue increased" in text


def test_inline_whitespace_collapsed():
    raw = "<p>a      b\t\tc</p>"
    text = clean.html_to_text(raw)
    assert "a b c" in text


def test_blank_lines_collapsed_to_at_most_one():
    raw = "<p>first</p>\n\n\n\n\n<p>second</p>"
    text = clean.html_to_text(raw)
    assert "\n\n\n" not in text
    assert "first" in text and "second" in text


def test_result_is_stripped_of_leading_trailing_whitespace():
    raw = "   <p>content</p>   "
    text = clean.html_to_text(raw)
    assert text == text.strip()
    assert text.startswith("content")


def test_script_before_and_after_content_both_removed():
    raw = "<script>a()</script><p>keep</p><script>b()</script>"
    text = clean.html_to_text(raw)
    assert text.strip() == "keep"


def test_count_tokens_approx_is_length_over_four():
    assert clean.count_tokens_approx("") == 0
    assert clean.count_tokens_approx("abcd") == 1
    assert clean.count_tokens_approx("a" * 100) == 25


def test_count_tokens_approx_no_tokenizer_dependency_roundoff():
    # 4 chars/token, integer division - a partial token doesn't round up.
    assert clean.count_tokens_approx("abcdefg") == 1  # 7 // 4 == 1
