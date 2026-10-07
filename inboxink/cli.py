"""The `inboxink` command: setup, run, update, doctor, uninstall."""
import argparse, sys

from . import __version__


def _run(args):
    from . import config, run, updater
    rc = run.execute(args)
    if not args.dry_run:
        try:  # at most once a day; never lets an update problem change the run's result
            updater.after_run(config.get())
        except Exception:
            pass
    return rc


def _setup(args):
    from . import setup_wizard
    return setup_wizard.execute(args)


def _update(args):
    from . import config, updater
    try:
        return updater.execute(args)
    except config.ConfigError as e:
        print(f"Cannot update yet: {e}", file=sys.stderr)
        return 2


def _doctor(args):
    from . import doctor
    return doctor.execute(args)


def _uninstall(args):
    from . import setup_wizard
    return setup_wizard.run_uninstall()


def build_parser():
    from . import run

    ap = argparse.ArgumentParser(prog="inboxink", description="Newsletters from your inbox to your e-reader.")
    ap.add_argument("--version", action="version", version=f"inboxink {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="<command>")
    p = sub.add_parser("run", help="deliver pending newsletters (what the schedule runs)")
    run.add_arguments(p)
    p.set_defaults(func=_run)
    sub.add_parser("setup", help="guided first-time setup (safe to run again)").set_defaults(func=_setup)
    p = sub.add_parser("update", help="update InboxInk and redeploy the Worker if needed")
    p.add_argument("--skip-upgrade", action="store_true", help=argparse.SUPPRESS)  # internal: second half of an update
    p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)  # internal: the daily update inside a run
    p.set_defaults(func=_update)
    sub.add_parser("doctor", help="check that everything is working").set_defaults(func=_doctor)
    sub.add_parser("uninstall", help="remove the schedule (and, if you say so, everything else)").set_defaults(func=_uninstall)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
