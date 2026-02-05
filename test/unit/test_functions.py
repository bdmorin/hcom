#!/usr/bin/env python3
"""Unit tests for pure utility functions in HCOM.

Tests UI utilities (ANSI handling, text input editing).
Isolated function tests for fast feedback and edge case coverage.

Run: pytest test/unit/test_functions.py -v
"""

from hypothesis import given, strategies as st
import pytest

from hcom.ui import (
    ansi_len,
    truncate_ansi,
    smart_truncate_name,
    text_input_insert,
    text_input_backspace,
    text_input_move_left,
    text_input_move_right,
)


# ==================== UI Utility Tests ====================
# Note: Log parsing tests removed - better tested at integration level
# Note: Color interpolation tests removed - test aesthetics, not functionality
# Note: Row calculation tests removed - implementation detail

class TestAnsiLen:
    """Test ANSI-aware length calculation"""

    def test_plain_text(self):
        """Plain text length is correct"""
        assert ansi_len("hello") == 5
        assert ansi_len("test") == 4
        assert ansi_len("") == 0

    def test_ansi_codes_excluded(self):
        """ANSI escape codes don't contribute to length"""
        assert ansi_len("\033[31mred\033[0m") == 3  # "red"
        assert ansi_len("\033[1mbold\033[0m") == 4  # "bold"

    def test_wide_chars_counted_correctly(self):
        """Wide characters (CJK, emoji) counted as 2"""
        assert ansi_len("日本") == 4  # 2 wide chars = 4 width
        assert ansi_len("🎉") == 2  # Emoji = 2 width

    def test_mixed_content(self):
        """Mixed ANSI codes and wide chars handled correctly"""
        assert ansi_len("\033[31m日\033[0m") == 2  # Colored wide char

    @given(st.text())
    def test_never_crashes(self, text):
        """ansi_len should never crash"""
        ansi_len(text)


class TestTruncateAnsi:
    """Test ANSI-aware truncation"""

    def test_no_truncation_needed(self):
        """Text shorter than width not truncated"""
        assert truncate_ansi("hello", 10) == "hello"

    def test_plain_text_truncation(self):
        """Plain text truncated with ellipsis"""
        result = truncate_ansi("hello world", 8)
        # ANSI codes add bytes beyond visual width - that's expected
        assert "…" in result
        # Visual width should be <= 8
        assert ansi_len(result) <= 8

    def test_preserves_ansi_codes(self):
        """ANSI codes preserved in truncated text"""
        result = truncate_ansi("\033[31mred text here\033[0m", 8)
        assert "\033[31m" in result  # Color code preserved
        assert "…" in result

    def test_zero_width(self):
        """Zero width returns empty"""
        assert truncate_ansi("hello", 0) == ""

    def test_wide_chars_truncation(self):
        """Wide characters truncated correctly"""
        result = truncate_ansi("日本語", 3)
        # Should truncate to fit within width, accounting for 2-width chars
        assert ansi_len(result) <= 3

    @given(st.text(max_size=100), st.integers(min_value=0, max_value=50))
    def test_never_crashes(self, text, width):
        """truncate_ansi should never crash"""
        truncate_ansi(text, width)


class TestSmartTruncateName:
    """Test intelligent name truncation"""

    def test_no_truncation_needed(self):
        """Short names not truncated"""
        assert smart_truncate_name("alice", 10) == "alice"
        assert smart_truncate_name("test", 10) == "test"

    def test_middle_ellipsis(self):
        """Long names truncated with middle ellipsis"""
        result = smart_truncate_name("alice_general-purpose_2", 11)
        assert "…" in result
        assert len(result) == 11
        # Should preserve prefix and suffix
        assert result.startswith("alice")
        assert result.endswith("2")

    def test_very_short_width(self):
        """Very short width still works"""
        result = smart_truncate_name("longname", 5)
        assert len(result) == 5

    @given(st.text(min_size=1, max_size=50), st.integers(min_value=1, max_value=100))
    def test_result_fits_width(self, name, width):
        """Truncated result always fits within width"""
        result = smart_truncate_name(name, width)
        assert len(result) <= width


class TestTextInputEditing:
    """Test text input editing functions"""

    def test_insert_at_cursor(self):
        """Insert text at cursor position"""
        buffer, cursor = text_input_insert("hello", 2, "X")
        assert buffer == "heXllo"
        assert cursor == 3

    def test_insert_at_start(self):
        """Insert at start works"""
        buffer, cursor = text_input_insert("world", 0, "hello ")
        assert buffer == "hello world"
        assert cursor == 6

    def test_insert_at_end(self):
        """Insert at end works"""
        buffer, cursor = text_input_insert("hello", 5, " world")
        assert buffer == "hello world"
        assert cursor == 11

    def test_backspace_middle(self):
        """Backspace in middle deletes char before cursor"""
        buffer, cursor = text_input_backspace("hello", 3)
        assert buffer == "helo"
        assert cursor == 2

    def test_backspace_at_start(self):
        """Backspace at start does nothing"""
        buffer, cursor = text_input_backspace("hello", 0)
        assert buffer == "hello"
        assert cursor == 0

    def test_move_left(self):
        """Move cursor left"""
        assert text_input_move_left(5) == 4
        assert text_input_move_left(0) == 0  # Can't go negative

    def test_move_right(self):
        """Move cursor right"""
        assert text_input_move_right("hello", 3) == 4
        assert text_input_move_right("hello", 5) == 5  # Can't go past end


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
