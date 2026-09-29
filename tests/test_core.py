import os
import tempfile
import unittest

from clinic.core import (
    ClinicGame,
    InventoryFull,
    InvalidState,
    MedicineExpired,
    OutOfStock,
    QueueEmpty,
    UnknownPatient,
)


def make_game():
    game = ClinicGame()
    game.add_medicine("aspirin", stock=5, capacity=10)
    game.add_medicine("antibiotic", stock=2, capacity=4, expires_at=2)
    game.add_doctor("dr_wong", slots=["09:00", "10:00"])
    return game


class SaveMixin:
    def save_path(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path


class TriageOrderTests(unittest.TestCase):
    def test_emergency_ordered_by_arrival_not_latest_first(self):
        game = make_game()
        first = game.arrive("alice", "emergency")
        second = game.arrive("bob", "emergency")
        # The later arrival must not jump ahead of the earlier emergency.
        self.assertEqual(game.queue, [first, second])
        self.assertEqual(game.next_patient(), first)

    def test_same_priority_keeps_stable_ticket_order(self):
        game = make_game()
        tickets = [game.arrive("p%d" % i, "normal") for i in range(4)]
        self.assertEqual(game.queue, sorted(tickets))
        urgent = game.arrive("urgent_guy", "urgent")
        emergency = game.arrive("emergency_guy", "emergency")
        self.assertEqual(game.queue[0], emergency)
        self.assertEqual(game.queue[1], urgent)
        self.assertEqual(game.queue[2:], sorted(tickets))


class TreatmentTests(unittest.TestCase):
    def test_retry_after_failure_does_not_deduct_medicine_again(self):
        game = make_game()
        ticket = game.arrive("alice")
        game.start_treatment(ticket, "aspirin")
        self.assertEqual(game.medicines["aspirin"]["stock"], 4)
        game.fail_treatment(ticket)
        game.retry_treatment(ticket)
        # The retry reuses the already-consumed dose.
        self.assertEqual(game.medicines["aspirin"]["stock"], 4)
        self.assertFalse(game.treatments[ticket]["failed"])
        # The retried treatment still completes and settles exactly once.
        for _ in range(3):
            game.tick()
        self.assertEqual(game.patients[ticket]["status"], "treated")
        self.assertEqual(game.settled, [ticket])

    def test_out_of_stock_rejected_without_state_change(self):
        game = make_game()
        game.medicines["antibiotic"]["stock"] = 0
        ticket = game.arrive("alice")
        before = game.to_dict()
        with self.assertRaises(OutOfStock):
            game.start_treatment(ticket, "antibiotic")
        self.assertEqual(game.to_dict(), before)


class ReferralTests(unittest.TestCase):
    def test_referral_removes_patient_from_waiting_queue(self):
        game = make_game()
        ticket = game.arrive("alice")
        other = game.arrive("bob")
        game.refer(ticket)
        self.assertNotIn(ticket, game.queue)
        self.assertEqual(game.queue, [other])
        self.assertEqual(game.patients[ticket]["status"], "referred")
        with self.assertRaises(InvalidState):
            game.refer(ticket)


class AppointmentTests(unittest.TestCase):
    def test_off_duty_releases_appointment_slots(self):
        game = make_game()
        ticket = game.arrive("alice")
        game.book_appointment("dr_wong", "09:00", ticket)
        self.assertEqual(game.doctors["dr_wong"]["slots"]["09:00"], ticket)
        game.doctor_off_duty("dr_wong")
        # Leaving shift frees every slot the doctor held.
        self.assertIsNone(game.doctors["dr_wong"]["slots"]["09:00"])
        self.assertIsNone(game.doctors["dr_wong"]["slots"]["10:00"])
        game.doctor_on_duty("dr_wong")
        game.book_appointment("dr_wong", "09:00", ticket)
        self.assertEqual(game.doctors["dr_wong"]["slots"]["09:00"], ticket)


class ExpiryTests(unittest.TestCase):
    def test_expired_medicine_cannot_be_used(self):
        game = make_game()
        ticket = game.arrive("alice")
        game.tick()
        game.tick()  # antibiotic expires at turn 2
        before = game.to_dict()
        with self.assertRaises(MedicineExpired):
            game.start_treatment(ticket, "antibiotic")
        # Failed command: no state change at all.
        self.assertEqual(game.to_dict(), before)
        self.assertEqual(game.medicines["antibiotic"]["stock"], 2)
        self.assertEqual(game.patients[ticket]["status"], "waiting")
        # Non-expired medicine still works.
        game.start_treatment(ticket, "aspirin")
        self.assertEqual(game.medicines["aspirin"]["stock"], 4)


class PersistenceTests(SaveMixin, unittest.TestCase):
    def test_save_includes_in_treatment_timers(self):
        path = self.save_path()
        game = make_game()
        ticket = game.arrive("alice")
        game.start_treatment(ticket, "aspirin", turns=3)
        game.tick()
        self.assertEqual(game.treatments[ticket]["remaining"], 2)
        game.save(path)
        restored = ClinicGame.load(path)
        # The in-progress timer survives the round trip.
        self.assertEqual(restored.treatments[ticket]["remaining"], 2)
        restored.tick()
        restored.tick()
        self.assertEqual(restored.patients[ticket]["status"], "treated")
        self.assertEqual(restored.settled, [ticket])

    def test_no_duplicate_settlement_after_restore(self):
        path = self.save_path()
        game = make_game()
        ticket = game.arrive("alice")
        game.start_treatment(ticket, "aspirin", turns=1)
        game.tick()
        self.assertEqual(game.settled, [ticket])
        game.save(path)
        restored = ClinicGame.load(path)
        # Extra ticks and explicit settle calls must not bill twice.
        restored.tick()
        restored.settle(ticket)
        self.assertEqual(restored.settled, [ticket])
        bills = [r for r in restored.ledger if r["ticket"] == ticket]
        self.assertEqual(len(bills), 1)


class BoundaryTests(unittest.TestCase):
    def test_empty_queue_boundaries(self):
        game = make_game()
        before = game.to_dict()
        with self.assertRaises(QueueEmpty):
            game.next_patient()
        with self.assertRaises(UnknownPatient):
            game.refer(99)
        with self.assertRaises(UnknownPatient):
            game.start_treatment(99, "aspirin")
        # Nothing mutated while the queue was empty.
        self.assertEqual(game.to_dict(), before)
        # The clinic still accepts arrivals afterwards.
        ticket = game.arrive("alice")
        self.assertEqual(game.next_patient(), ticket)

    def test_full_inventory_boundary(self):
        game = make_game()
        game.restock("aspirin", 5)
        self.assertEqual(game.medicines["aspirin"]["stock"], 10)
        before = game.to_dict()
        with self.assertRaises(InventoryFull):
            game.restock("aspirin", 1)
        # Overflow rejected: stock stays exactly at capacity.
        self.assertEqual(game.medicines["aspirin"]["stock"], 10)
        self.assertEqual(game.to_dict(), before)
        # Consuming one dose makes room for exactly one restock.
        ticket = game.arrive("alice")
        game.start_treatment(ticket, "aspirin")
        game.restock("aspirin", 1)
        self.assertEqual(game.medicines["aspirin"]["stock"], 10)


if __name__ == "__main__":
    unittest.main()
