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

from enum import IntEnum

import attr

from hypothesis.errors import Frozen, InvalidArgument, StopTest
from hypothesis.internal.compat import (
    benchmark_time,
    bit_length,
    hbytes,
    int_from_bytes,
    int_to_bytes,
    text_type,
    unicode_safe_repr,
)
from hypothesis.internal.conjecture.utils import calc_label_from_name
from hypothesis.internal.escalation import mark_for_escalation

TOP_LABEL = calc_label_from_name("top")
DRAW_BYTES_LABEL = calc_label_from_name("draw_bytes() in ConjectureData")


class Status(IntEnum):
    OVERRUN = 0
    INVALID = 1
    VALID = 2
    INTERESTING = 3

    def __repr__(self):
        return "Status.%s" % (self.name,)


@attr.s(slots=True)
class Example(object):
    """Examples track the hierarchical structure of draws from the byte stream,
    within a single test run.

    Examples are created to mark regions of the byte stream that might be
    useful to the shrinker, such as:
    - The bytes used by a single draw from a strategy.
    - Useful groupings within a strategy, such as individual list elements.
    - Strategy-like helper functions that aren't first-class strategies.
    - Each lowest-level draw of bits or bytes from the byte stream.
    - A single top-level example that spans the entire input.

    Example-tracking allows the shrinker to try "high-level" transformations,
    such as rearranging or deleting the elements of a list, without having
    to understand their exact representation in the byte stream.
    """

    # Depth of this example in the example tree. The top-level example has a
    # depth of 0.
    depth = attr.ib(repr=False)

    # A label is an opaque value that associates each example with its
    # approximate origin, such as a particular strategy class or a particular
    # kind of draw.
    label = attr.ib()

    # Index of this example inside the overall list of examples.
    index = attr.ib()

    start = attr.ib()
    end = attr.ib(default=None)

    # An example is "trivial" if it only contains forced bytes and zero bytes.
    # All examples start out as trivial, and then get marked non-trivial when
    # we see a byte that is neither forced nor zero.
    trivial = attr.ib(default=True, repr=False)

    # True if we believe that the shrinker should be able to delete this
    # example completely, without affecting the value produced by its enclosing
    # strategy. Typically set when a rejection sampler decides to reject a
    # generated value and try again.
    discarded = attr.ib(default=None, repr=False)

    # List of child examples, represented as indices into the example list.
    children = attr.ib(default=attr.Factory(list), repr=False)

    @property
    def length(self):
        return self.end - self.start


@attr.s(slots=True, frozen=True)
class Block(object):
    """Blocks track the flat list of lowest-level draws from the byte stream,
    within a single test run.

    Block-tracking allows the shrinker to try "low-level"
    transformations, such as minimizing the numeric value of an
    individual call to ``draw_bits``.
    """

    start = attr.ib()
    end = attr.ib()

    # How many bits this block was drawn with.
    bits = attr.ib()

    # Index of this block inside the overall list of blocks.
    index = attr.ib()

    # True if this block's byte values were forced by a write operation.
    # As long as the bytes before this block remain the same, modifying this
    # block's bytes will have no effect.
    forced = attr.ib(repr=False)

    # True if this block's byte values are all 0. Reading this flag can be
    # more convenient than explicitly checking a slice for non-zero bytes.
    all_zero = attr.ib(repr=False)

    @property
    def bounds(self):
        return (self.start, self.end)

    @property
    def length(self):
        return self.end - self.start

    @property
    def trivial(self):
        return self.forced or self.all_zero


class _Overrun(object):
    status = Status.OVERRUN

    def __repr__(self):
        return "Overrun"


Overrun = _Overrun()

global_test_counter = 0


MAX_DEPTH = 100


class ConjectureData(object):
    @classmethod
    def for_buffer(self, buffer):
        buffer = hbytes(buffer)
        return ConjectureData(
            max_length=len(buffer),
            draw_bytes=lambda data, n: hbytes(buffer[data.index : data.index + n]),
        )

    def __init__(self, max_length, draw_bytes):
        self.max_length = max_length
        self.is_find = False
        self._draw_bytes = draw_bytes
        self.overdraw = 0
        self.block_starts = {}
        self.blocks = []
        self.buffer = bytearray()
        self.output = u""
        self.status = Status.VALID
        self.frozen = False
        global global_test_counter
        self.testcounter = global_test_counter
        global_test_counter += 1
        self.start_time = benchmark_time()
        self.events = set()
        self.interesting_origin = None
        self.draw_times = []
        self.max_depth = 0

        self.examples = []
        self.example_stack = []
        self.has_discards = False

        top = self.start_example(TOP_LABEL)
        assert top.depth == 0

    def __repr__(self):
        return "ConjectureData(%s, %d bytes%s)" % (
            self.status.name,
            len(self.buffer),
            ", frozen" if self.frozen else "",
        )

    def __assert_not_frozen(self, name):
        if self.frozen:
            raise Frozen("Cannot call %s on frozen ConjectureData" % (name,))

    @property
    def depth(self):
        # We always have a single example wrapping everything. We want to treat
        # that as depth 0 rather than depth 1.
        return len(self.example_stack) - 1

    @property
    def index(self):
        return len(self.buffer)

    def all_block_bounds(self):
        return [block.bounds for block in self.blocks]

    def note(self, value):
        self.__assert_not_frozen("note")
        if not isinstance(value, text_type):
            value = unicode_safe_repr(value)
        self.output += value

    def draw(self, strategy, label=None):
        if self.is_find and not strategy.supports_find:
            raise InvalidArgument(
                (
                    "Cannot use strategy %r within a call to find (presumably "
                    "because it would be invalid after the call had ended)."
                )
                % (strategy,)
            )

        if strategy.is_empty:
            self.mark_invalid()

        if self.depth >= MAX_DEPTH:
            self.mark_invalid()

        return self.__draw(strategy, label=label)

    def __draw(self, strategy, label):
        at_top_level = self.depth == 0
        if label is None:
            label = strategy.label
        self.start_example(label=label)
        try:
            if not at_top_level:
                return strategy.do_draw(self)
            else:
                try:
                    strategy.validate()
                    start_time = benchmark_time()
                    try:
                        return strategy.do_draw(self)
                    finally:
                        self.draw_times.append(benchmark_time() - start_time)
                except BaseException as e:
                    mark_for_escalation(e)
                    raise
        finally:
            self.stop_example()

    def start_example(self, label):
        self.__assert_not_frozen("start_example")

        i = len(self.examples)
        new_depth = self.depth + 1
        ex = Example(index=i, depth=new_depth, label=label, start=self.index)
        self.examples.append(ex)
        if self.example_stack:
            p = self.example_stack[-1]
            self.examples[p].children.append(ex)
        self.example_stack.append(i)
        self.max_depth = max(self.max_depth, self.depth)
        return ex

    def stop_example(self, discard=False):
        if self.frozen:
            return

        k = self.example_stack.pop()
        ex = self.examples[k]
        ex.end = self.index

        if self.example_stack and not ex.trivial:
            self.examples[self.example_stack[-1]].trivial = False

        # We don't want to count empty examples as discards even if the flag
        # says we should. This leads to situations like
        # https://github.com/HypothesisWorks/hypothesis/issues/1230
        # where it can look like we should discard data but there's nothing
        # useful for us to do.
        if self.index == ex.start:
            discard = False

        ex.discarded = discard

        if discard:
            self.has_discards = True

    def note_event(self, event):
        self.events.add(event)

    def freeze(self):
        if self.frozen:
            assert isinstance(self.buffer, hbytes)
            return
        self.finish_time = benchmark_time()

        while self.example_stack:
            self.stop_example()

        self.frozen = True

        if self.status >= Status.VALID:
            discards = []
            for ex in self.examples:
                if ex.length == 0:
                    continue
                if discards:
                    u, v = discards[-1]
                    if u <= ex.start <= ex.end <= v:
                        continue
                if ex.discarded:
                    discards.append((ex.start, ex.end))
                    continue

        self.buffer = hbytes(self.buffer)
        self.events = frozenset(self.events)
        del self._draw_bytes

    def blocks_with_values(self):
        for b in self.blocks:
            n = int_from_bytes(self.buffer[b.start : b.end])
            assert bit_length(n) <= b.bits
            yield (b, n)

    def draw_bits(self, n, forced=None):
        """Return an ``n``-bit integer from the underlying source of
        bytes. If ``forced`` is set to an integer will instead
        ignore the underlying source and simulate a draw as if it had
        returned that integer."""
        self.__assert_not_frozen("draw_bits")
        if n == 0:
            result = 0
        n_bytes = bits_to_bytes(n)
        self.__check_capacity(n_bytes)

        if forced is not None:
            buf = bytearray(int_to_bytes(forced, n_bytes))
        else:
            buf = bytearray(self._draw_bytes(self, n_bytes))
        assert len(buf) == n_bytes

        # If we have a number of bits that is not a multiple of 8
        # we have to mask off the high bits.
        if n % 8 != 0:
            mask = (1 << (n % 8)) - 1
            assert mask != 0
            buf[0] &= mask
        buf = hbytes(buf)
        result = int_from_bytes(buf)

        ex = self.start_example(DRAW_BYTES_LABEL)
        initial = self.index

        block = Block(
            start=initial,
            end=initial + n_bytes,
            bits=n,
            index=len(self.blocks),
            forced=forced is not None,
            all_zero=result == 0,
        )
        ex.trivial = block.trivial
        self.block_starts.setdefault(n_bytes, []).append(block.start)
        self.blocks.append(block)
        assert self.blocks[block.index] is block
        assert self.index == initial
        self.buffer.extend(buf)
        self.stop_example()

        assert bit_length(result) <= n
        return result

    def draw_bytes(self, n):
        if n == 0:
            return hbytes(b"")
        return int_to_bytes(self.draw_bits(8 * n), n)

    def write(self, string):
        self.__assert_not_frozen("write")
        string = hbytes(string)
        if not string:
            return
        self.draw_bits(len(string) * 8, forced=int_from_bytes(string))
        return self.buffer[-len(string) :]

    def __check_capacity(self, n):
        if self.index + n > self.max_length:
            self.overdraw = self.index + n - self.max_length
            self.status = Status.OVERRUN
            self.freeze()
            raise StopTest(self.testcounter)

    def conclude_test(self, status, interesting_origin=None):
        assert (interesting_origin is None) or (status == Status.INTERESTING)
        self.__assert_not_frozen("conclude_test")
        self.interesting_origin = interesting_origin
        self.status = status
        self.freeze()
        raise StopTest(self.testcounter)

    def mark_interesting(self, interesting_origin=None):
        self.conclude_test(Status.INTERESTING, interesting_origin)

    def mark_invalid(self):
        self.conclude_test(Status.INVALID)


def bits_to_bytes(n):
    n_bytes = n // 8
    if n % 8 != 0:
        n_bytes += 1
    return n_bytes
