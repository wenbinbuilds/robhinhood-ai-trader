from enum import IntEnum


class EntryAction(IntEnum):
    WAIT = 0
    ENTER = 1
    IGNORE_SETUP = 2


class PositionAction(IntEnum):
    HOLD = 0
    EXIT = 1
