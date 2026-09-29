"""Clinic triage game: core state machine.

State-flow invariants enforced by this module:

* The waiting queue is always ordered by (priority rank, ticket number).
  Tickets are issued monotonically on arrival, so patients with the same
  priority keep a stable first-come-first-served order and a later
  emergency never jumps ahead of an earlier one.
* A treatment session consumes its medicine dose exactly once, when the
  session starts; retrying a failed session never deducts stock again.
* Referring a patient removes them from the waiting queue immediately.
* Taking a doctor off duty releases every appointment slot they held.
* Expired medicine can never be dispensed.
* Save files include in-progress treatment timers.
* Settlement is idempotent per patient, even across a save/restore cycle.
* Every command validates first and mutates afterwards, so a rejected
  command leaves the state untouched (exactly one logical state change
  per successful command).
"""

import json

SAVE_VERSION = 1

PRIORITIES = ("emergency", "urgent", "normal")
_PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}

DEFAULT_TREATMENT_TURNS = 3

WAITING = "waiting"
IN_TREATMENT = "in_treatment"
REFERRED = "referred"
TREATED = "treated"


class ClinicError(Exception):
    """Base class for all clinic command failures."""


class UnknownPatient(ClinicError):
    pass


class UnknownMedicine(ClinicError):
    pass


class UnknownDoctor(ClinicError):
    pass


class QueueEmpty(ClinicError):
    pass


class OutOfStock(ClinicError):
    pass


class MedicineExpired(ClinicError):
    pass


class InventoryFull(ClinicError):
    pass


class InvalidState(ClinicError):
    pass


class ClinicGame:
    """Mutable game state plus the command methods that mutate it."""

    def __init__(self):
        self.turn = 0
        self.next_ticket = 1
        self.patients = {}      # ticket -> patient dict
        self.queue = []         # tickets ordered by (priority rank, ticket)
        self.medicines = {}     # name -> {stock, capacity, expires_at}
        self.doctors = {}       # name -> {on_duty, slots: {slot: ticket|None}}
        self.treatments = {}    # ticket -> {medicine, remaining, failed, dose_taken}
        self.ledger = []        # billing records, at most one per patient
        self.settled = []       # tickets already billed (idempotency guard)

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------
    def _patient(self, ticket):
        try:
            return self.patients[int(ticket)]
        except (KeyError, ValueError, TypeError):
            raise UnknownPatient("no patient with ticket %r" % (ticket,))

    def _medicine(self, name):
        try:
            return self.medicines[name]
        except KeyError:
            raise UnknownMedicine("no medicine named %r" % (name,))

    def _doctor(self, name):
        try:
            return self.doctors[name]
        except KeyError:
            raise UnknownDoctor("no doctor named %r" % (name,))

    def _medicine_expired(self, med):
        return med["expires_at"] is not None and self.turn >= med["expires_at"]

    # ------------------------------------------------------------------
    # registration commands
    # ------------------------------------------------------------------
    def add_medicine(self, name, stock, capacity, expires_at=None):
        if name in self.medicines:
            raise InvalidState("medicine %r already registered" % (name,))
        if stock < 0 or capacity < 0 or stock > capacity:
            raise InvalidState("stock must satisfy 0 <= stock <= capacity")
        self.medicines[name] = {
            "stock": stock,
            "capacity": capacity,
            "expires_at": expires_at,
        }

    def add_doctor(self, name, slots):
        if name in self.doctors:
            raise InvalidState("doctor %r already registered" % (name,))
        self.doctors[name] = {
            "on_duty": True,
            "slots": {slot: None for slot in slots},
        }

    # ------------------------------------------------------------------
    # triage queue commands
    # ------------------------------------------------------------------
    def arrive(self, name, priority="normal"):
        if priority not in _PRIORITY_RANK:
            raise InvalidState("unknown priority %r" % (priority,))
        ticket = self.next_ticket
        self.next_ticket += 1
        self.patients[ticket] = {
            "ticket": ticket,
            "name": name,
            "priority": priority,
            "status": WAITING,
        }
        self.queue.append(ticket)
        # Python's sort is stable, and tickets grow monotonically, so
        # sorting by rank alone keeps same-priority patients in arrival
        # (ticket) order; include the ticket as tiebreaker to be explicit.
        self.queue.sort(
            key=lambda t: (_PRIORITY_RANK[self.patients[t]["priority"]], t)
        )
        return ticket

    def next_patient(self):
        if not self.queue:
            raise QueueEmpty("no patients waiting")
        return self.queue[0]

    def refer(self, ticket):
        patient = self._patient(ticket)
        if patient["status"] != WAITING:
            raise InvalidState("only a waiting patient can be referred")
        patient["status"] = REFERRED
        self.queue.remove(patient["ticket"])

    # ------------------------------------------------------------------
    # appointment commands
    # ------------------------------------------------------------------
    def book_appointment(self, doctor_name, slot, ticket):
        doctor = self._doctor(doctor_name)
        patient = self._patient(ticket)
        if not doctor["on_duty"]:
            raise InvalidState("doctor %r is off duty" % (doctor_name,))
        if slot not in doctor["slots"]:
            raise InvalidState("doctor %r has no slot %r" % (doctor_name, slot))
        if doctor["slots"][slot] is not None:
            raise InvalidState("slot %r is already booked" % (slot,))
        doctor["slots"][slot] = patient["ticket"]

    def doctor_off_duty(self, name):
        doctor = self._doctor(name)
        if not doctor["on_duty"]:
            raise InvalidState("doctor %r is already off duty" % (name,))
        doctor["on_duty"] = False
        # Leaving shift releases every appointment slot the doctor held.
        for slot in doctor["slots"]:
            doctor["slots"][slot] = None

    def doctor_on_duty(self, name):
        doctor = self._doctor(name)
        if doctor["on_duty"]:
            raise InvalidState("doctor %r is already on duty" % (name,))
        doctor["on_duty"] = True

    # ------------------------------------------------------------------
    # pharmacy commands
    # ------------------------------------------------------------------
    def restock(self, name, amount):
        med = self._medicine(name)
        if amount <= 0:
            raise InvalidState("restock amount must be positive")
        if med["stock"] + amount > med["capacity"]:
            raise InventoryFull(
                "restocking %d would exceed capacity %d"
                % (amount, med["capacity"])
            )
        med["stock"] += amount

    # ------------------------------------------------------------------
    # treatment commands
    # ------------------------------------------------------------------
    def start_treatment(self, ticket, medicine, turns=DEFAULT_TREATMENT_TURNS):
        patient = self._patient(ticket)
        med = self._medicine(medicine)
        if patient["status"] != WAITING:
            raise InvalidState("only a waiting patient can start treatment")
        if self._medicine_expired(med):
            raise MedicineExpired("medicine %r is expired" % (medicine,))
        if med["stock"] <= 0:
            raise OutOfStock("medicine %r is out of stock" % (medicine,))
        # All checks passed: consume exactly one dose and start the timer.
        med["stock"] -= 1
        patient["status"] = IN_TREATMENT
        self.queue.remove(patient["ticket"])
        self.treatments[patient["ticket"]] = {
            "medicine": medicine,
            "remaining": turns,
            "failed": False,
            "dose_taken": True,
        }

    def fail_treatment(self, ticket):
        patient = self._patient(ticket)
        session = self.treatments.get(patient["ticket"])
        if session is None:
            raise InvalidState(
                "patient %d is not in treatment" % (patient["ticket"],)
            )
        if session["failed"]:
            raise InvalidState("treatment already marked failed")
        session["failed"] = True

    def retry_treatment(self, ticket, turns=DEFAULT_TREATMENT_TURNS):
        patient = self._patient(ticket)
        session = self.treatments.get(patient["ticket"])
        if session is None:
            raise InvalidState(
                "patient %d is not in treatment" % (patient["ticket"],)
            )
        if not session["failed"]:
            raise InvalidState("only a failed treatment can be retried")
        # The dose was consumed when the session started; the retry reuses
        # it, so stock is never deducted a second time.
        session["failed"] = False
        session["remaining"] = turns

    # ------------------------------------------------------------------
    # settlement
    # ------------------------------------------------------------------
    def _settle(self, ticket):
        # Idempotent: each patient is billed exactly once, even if
        # completion logic replays after a save/restore cycle.
        if ticket in self.settled:
            return False
        self.settled.append(ticket)
        self.ledger.append({"ticket": ticket, "turn": self.turn})
        return True

    def settle(self, ticket):
        patient = self._patient(ticket)
        if patient["status"] != TREATED:
            raise InvalidState("only a treated patient can be settled")
        return self._settle(patient["ticket"])

    # ------------------------------------------------------------------
    # clock
    # ------------------------------------------------------------------
    def tick(self):
        self.turn += 1
        for ticket in sorted(self.treatments):
            session = self.treatments[ticket]
            if session["failed"]:
                continue
            session["remaining"] -= 1
            if session["remaining"] <= 0:
                del self.treatments[ticket]
                self.patients[ticket]["status"] = TREATED
                self._settle(ticket)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def to_dict(self):
        return {
            "version": SAVE_VERSION,
            "turn": self.turn,
            "next_ticket": self.next_ticket,
            "patients": {str(t): p for t, p in self.patients.items()},
            "queue": list(self.queue),
            "medicines": self.medicines,
            "doctors": self.doctors,
            # In-progress treatment timers are part of the save file.
            "treatments": {str(t): s for t, s in self.treatments.items()},
            "ledger": list(self.ledger),
            "settled": list(self.settled),
        }

    @classmethod
    def from_dict(cls, data):
        if data.get("version") != SAVE_VERSION:
            raise InvalidState(
                "unsupported save version %r" % (data.get("version"),)
            )
        game = cls()
        game.turn = data["turn"]
        game.next_ticket = data["next_ticket"]
        game.patients = {int(t): p for t, p in data["patients"].items()}
        game.queue = list(data["queue"])
        game.medicines = {name: dict(m) for name, m in data["medicines"].items()}
        game.doctors = {
            name: {"on_duty": d["on_duty"], "slots": dict(d["slots"])}
            for name, d in data["doctors"].items()
        }
        game.treatments = {int(t): dict(s) for t, s in data["treatments"].items()}
        game.ledger = [dict(r) for r in data["ledger"]]
        game.settled = list(data["settled"])
        return game

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


def new_default_game():
    """A playable starting state for the terminal interface."""
    game = ClinicGame()
    game.add_medicine("aspirin", stock=5, capacity=10, expires_at=None)
    game.add_medicine("antibiotic", stock=3, capacity=6, expires_at=20)
    game.add_doctor("dr_wong", slots=["09:00", "10:00", "11:00"])
    game.add_doctor("dr_li", slots=["09:00", "10:00"])
    return game
