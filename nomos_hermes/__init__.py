# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Native Hermes authorization adapter for Nomos."""

from .plugin import register

__all__ = ["register"]
