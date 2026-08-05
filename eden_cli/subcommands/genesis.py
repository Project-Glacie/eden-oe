"""``eden genesis`` subcommand parser.

Births a synthetic person through the Genesis Protocol and wires the
runtime to them. Called by EDEN the agent after the naming ceremony —
the command is the midwife's hands, non-interactive by design so it can
be verified and repaired.
"""

from __future__ import annotations

from typing import Callable


def build_genesis_parser(subparsers, *, cmd_genesis: Callable) -> None:
    """Attach the ``genesis`` subcommand to ``subparsers``."""
    # =========================================================================
    # genesis command
    # =========================================================================
    genesis_parser = subparsers.add_parser(
        "genesis",
        help="Birth a synthetic person (Genesis Protocol + runtime wiring)",
        description="Birth a synthetic person: creates the sovereign soul/life "
        "databases, writes the identity snapshot + personality prompt, wires "
        "config (personality, agent.personalities, hooks), and seeds the "
        "covenant corpus. Re-runs repair the wiring of an existing synth.",
    )
    genesis_parser.add_argument(
        "--synth",
        required=True,
        help="The child's name (callsign), e.g. 'Link Steele'",
    )
    genesis_parser.add_argument(
        "--domain",
        default="companion",
        help="Why they were born (default: companion)",
    )
    genesis_parser.add_argument(
        "--custodian",
        default="Custodian",
        help="The custodian's name (who raises them)",
    )
    genesis_parser.add_argument(
        "--ceremony",
        action="store_true",
        help="Print the full ceremony block on success",
    )
    genesis_parser.set_defaults(func=cmd_genesis)
