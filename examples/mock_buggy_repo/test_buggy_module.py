import pytest

from buggy_module import divide


def test_divide_zero_raises():
    with pytest.raises(ZeroDivisionError):
        divide(10, 0)

