#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
intervaltree: A mutable, self-balancing interval tree for Python 2 and 3.
Queries may be by point, by range overlap, or by range envelopment.

Root package.

Copyright 2013-2018 Chaim Leib Halbert

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from .interval import Interval
from .intervaltree import IntervalTree
from .ledger import (
    Allocation,
    AllocationEntry,
    AllocationError,
    AmbiguousPulseError,
    InvalidLedgerError,
    Ledger,
    LedgerError,
    Period,
    Pulse,
    PulseAllocation,
    PulseAllocationError,
    PulseAssignment,
    PulseLedger,
    RemainderAward,
    SubledgerError,
    UnallocatedPulseError,
    UnassignedPulseError,
    allocate,
    allocate_pulses,
    apportion,
    apportion_pulses,
    distribute_pulses,
    pulse_ledger,
    split_ledger,
    split_pulses,
)
