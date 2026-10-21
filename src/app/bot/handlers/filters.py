"""Shared magic filters."""

from typing import Final

from aiogram import F
from aiogram.utils.magic_filter import MagicFilter

from app.bot.keyboards import MENU_TEXTS

TYPED_INPUT: Final[MagicFilter] = F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_TEXTS)
"""Free text that is neither a command nor a main-menu button (flows read these)."""
