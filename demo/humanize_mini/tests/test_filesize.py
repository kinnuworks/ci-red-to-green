"""Tests for filesize humanizing (from humanize's own test suite)."""

from __future__ import annotations

import pytest

from humanize_mini import naturalsize


@pytest.mark.parametrize(
    "test_args, expected",
    [
        ([300], "300 Bytes"),
        (["1000"], "1.0 kB"),
        ([10**6], "1.0 MB"),
        ([10**9], "1.0 GB"),
        ([1000**2 * 32], "32.0 MB"),
        ([1024**3 * 33, True], "33.0 GiB"),
        ([1000**3 * 33, False, True], "30.7G"),
        ([300, False, True], "300B"),
        ([3000, False, True], "2.9K"),
        ([1, False, False], "1 Byte"),
        ([3141592, False, False, "%.2f"], "3.14 MB"),
        ([-4096, True], "-4.0 KiB"),
        # Rounding must not leave the mantissa at the base while a larger suffix
        # is available: 999999 is 999.999 kB, which the "%.1f" format rounds to
        # 1000.0 and must carry into 1.0 MB rather than render as "1000.0 kB".
        ([999999], "1.0 MB"),
        ([999999999], "1.0 GB"),
        ([1024**2 - 1, True], "1.0 MiB"),
        ([1024**2 - 1, False, True], "1.0M"),
    ],
)
def test_naturalsize(test_args: list, expected: str) -> None:
    assert naturalsize(*test_args) == expected


def test_naturalsize_negative() -> None:
    assert naturalsize(-3000000) == "-3.0 MB"
