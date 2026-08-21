"""Dispatch table mapping a trigger kind to the composer that knows how to frame it."""

from __future__ import annotations

from typing import Callable

from ..facts import FactSheet
from ..insights import Insights
from .base import Plan

ComposerFn = Callable[[FactSheet, Insights], "Plan | None"]

MERCHANT_COMPOSERS: dict[str, ComposerFn] = {}
CUSTOMER_COMPOSERS: dict[str, ComposerFn] = {}


def merchant_kind(*kinds: str):
    def wrap(fn: ComposerFn) -> ComposerFn:
        for kind in kinds:
            MERCHANT_COMPOSERS[kind] = fn
        return fn
    return wrap


def customer_kind(*kinds: str):
    def wrap(fn: ComposerFn) -> ComposerFn:
        for kind in kinds:
            CUSTOMER_COMPOSERS[kind] = fn
        return fn
    return wrap
