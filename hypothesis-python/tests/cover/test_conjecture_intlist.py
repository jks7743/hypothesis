# coding=utf-8
#
# This file is part of Hypothesis, which may be found at
# https://github.com/HypothesisWorks/hypothesis/
#
# Most of this work is copyright (C) 2013-2019 David R. MacIver
# (david@drmaciver.com), but it contains contributions by others. See
# CONTRIBUTING.rst for a full list of people who may hold copyright, and
# consult the git log if you need to determine who owns an individual
# contribution.
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file, You can
# obtain one at https://mozilla.org/MPL/2.0/.
#
# END HEADER

from __future__ import absolute_import, division, print_function

import pytest

import hypothesis.strategies as st
from hypothesis import assume, given
from hypothesis.internal.compat import PY2
from hypothesis.internal.conjecture.junkdrawer import IntList

non_neg_lists = st.lists(st.integers(min_value=0, max_value=2 ** 63 - 1))


@given(non_neg_lists)
def test_intlist_is_equal_to_itself(ls):
    assert IntList(ls) == IntList(ls)


@given(non_neg_lists, non_neg_lists)
def test_distinct_int_lists_are_not_equal(x, y):
    assume(x != y)
    assert IntList(x) != IntList(y)


def test_basic_equality():
    x = IntList([1, 2, 3])
    assert x == x
    t = x != x
    assert not t
    assert x != "foo"

    s = x == "foo"
    assert not s


@pytest.mark.skipif(
    PY2,
    reason="The Python 2 list fallback handles this and we don't really care enough to validate it there.",
)
def test_error_on_invalid_value():
    with pytest.raises(ValueError):
        IntList([-1])
