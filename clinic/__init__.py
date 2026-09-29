"""Clinic triage game package."""

from .core import (
    ClinicError,
    ClinicGame,
    InventoryFull,
    InvalidState,
    MedicineExpired,
    OutOfStock,
    QueueEmpty,
    UnknownDoctor,
    UnknownMedicine,
    UnknownPatient,
    new_default_game,
)

__all__ = [
    "ClinicError",
    "ClinicGame",
    "InventoryFull",
    "InvalidState",
    "MedicineExpired",
    "OutOfStock",
    "QueueEmpty",
    "UnknownDoctor",
    "UnknownMedicine",
    "UnknownPatient",
    "new_default_game",
]
