#!/usr/bin/env python3
"""Print a greeting from the agent."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print a greeting from the agent."
    )
    parser.add_argument(
        "--loud",
        action="store_true",
        help="print the greeting in uppercase",
    )
    args = parser.parse_args()

    message = "hello from the agent"
    if args.loud:
        message = message.upper()

    print(message)


if __name__ == "__main__":
    main()
