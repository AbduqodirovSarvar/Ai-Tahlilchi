"""clip_text: the cut that keeps the call log bounded."""

from hypothesis import given
from hypothesis import strategies as st

from app.domain import MAX_LOGGED_TEXT, clip_text


@given(text=st.text(max_size=300), limit=st.integers(min_value=1, max_value=300))
def test_clip_text_keeps_a_prefix_and_marks_what_was_cut(text: str, limit: int) -> None:
    clipped = clip_text(text, limit)

    if len(text) <= limit:
        assert clipped == text
    else:
        assert clipped.startswith(text[:limit])
        assert f"{len(text) - limit} ta belgi qirqildi" in clipped
        assert len(clipped) < len(text) + 40


@given(text=st.text(max_size=300))
def test_clip_text_is_idempotent_below_the_limit(text: str) -> None:
    assert clip_text(clip_text(text)) == clip_text(text)


def test_clip_text_default_limit_is_the_contract_constant() -> None:
    assert clip_text("a" * MAX_LOGGED_TEXT) == "a" * MAX_LOGGED_TEXT
    assert clip_text("a" * (MAX_LOGGED_TEXT + 1)).startswith("a" * MAX_LOGGED_TEXT)
    assert clip_text("") == ""
