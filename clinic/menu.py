"""Terminal interface for the clinic triage game.

One command per line; every command maps to exactly one core state change.
Run with: python -m clinic
"""

from .core import ClinicError, ClinicGame, new_default_game

HELP = """commands:
  arrive <name> [priority]     register a patient (emergency|urgent|normal)
  queue                        show the waiting queue
  next                         show the next patient ticket
  refer <ticket>               refer a waiting patient away
  treat <ticket> <medicine>    start treatment (consumes one dose)
  fail <ticket>                mark an ongoing treatment as failed
  retry <ticket>               retry a failed treatment (no extra dose)
  book <doctor> <slot> <ticket>  book an appointment slot
  offduty <doctor>             doctor leaves shift (releases slots)
  onduty <doctor>              doctor returns to shift
  restock <medicine> <amount>  add stock (never above capacity)
  tick                         advance the clinic clock one turn
  status                       show medicines, doctors and ledger
  save <path>                  save game state as JSON
  load <path>                  load game state from JSON
  help                         show this help
  quit                         exit
"""


def _show_queue(game):
    if not game.queue:
        print("(queue is empty)")
        return
    for ticket in game.queue:
        patient = game.patients[ticket]
        print("#%d %s [%s]" % (ticket, patient["name"], patient["priority"]))


def _show_status(game):
    print("turn: %d" % game.turn)
    for name, med in sorted(game.medicines.items()):
        expiry = "never" if med["expires_at"] is None else str(med["expires_at"])
        print(
            "medicine %s: stock %d/%d, expires at %s"
            % (name, med["stock"], med["capacity"], expiry)
        )
    for name, doc in sorted(game.doctors.items()):
        duty = "on duty" if doc["on_duty"] else "off duty"
        slots = ", ".join(
            "%s->%s" % (slot, occupant if occupant is not None else "free")
            for slot, occupant in sorted(doc["slots"].items())
        )
        print("doctor %s: %s (%s)" % (name, duty, slots))
    for ticket, session in sorted(game.treatments.items()):
        state = "failed" if session["failed"] else "running"
        print(
            "treatment #%d: %s, %d turn(s) left, %s"
            % (ticket, session["medicine"], session["remaining"], state)
        )
    print("settled: %s" % (", ".join("#%d" % t for t in game.settled) or "none"))


def run_command(game, line):
    """Execute one command line; returns False to exit the loop."""
    parts = line.split()
    if not parts:
        return True
    command, args = parts[0], parts[1:]
    try:
        if command == "arrive":
            ticket = game.arrive(args[0], args[1] if len(args) > 1 else "normal")
            print("ticket #%d" % ticket)
        elif command == "queue":
            _show_queue(game)
        elif command == "next":
            print("next ticket: #%d" % game.next_patient())
        elif command == "refer":
            game.refer(int(args[0]))
            print("referred #%s" % args[0])
        elif command == "treat":
            game.start_treatment(int(args[0]), args[1])
            print("treatment started for #%s" % args[0])
        elif command == "fail":
            game.fail_treatment(int(args[0]))
            print("treatment failed for #%s" % args[0])
        elif command == "retry":
            game.retry_treatment(int(args[0]))
            print("treatment retried for #%s" % args[0])
        elif command == "book":
            game.book_appointment(args[0], args[1], int(args[2]))
            print("booked %s %s for #%s" % (args[0], args[1], args[2]))
        elif command == "offduty":
            game.doctor_off_duty(args[0])
            print("%s is off duty" % args[0])
        elif command == "onduty":
            game.doctor_on_duty(args[0])
            print("%s is on duty" % args[0])
        elif command == "restock":
            game.restock(args[0], int(args[1]))
            print("restocked %s" % args[0])
        elif command == "tick":
            game.tick()
            print("turn %d" % game.turn)
        elif command == "status":
            _show_status(game)
        elif command == "save":
            game.save(args[0])
            print("saved to %s" % args[0])
        elif command == "load":
            loaded = ClinicGame.load(args[0])
            game.__dict__.update(loaded.__dict__)
            print("loaded from %s" % args[0])
        elif command == "help":
            print(HELP)
        elif command in ("quit", "exit"):
            return False
        else:
            print("unknown command %r; type 'help'" % command)
    except (ClinicError, IndexError, ValueError) as exc:
        print("error: %s" % exc)
    return True


def main():
    game = new_default_game()
    print("clinic triage game; type 'help' for commands")
    while True:
        try:
            line = input("clinic> ")
        except EOFError:
            break
        if not run_command(game, line):
            break


if __name__ == "__main__":
    main()
