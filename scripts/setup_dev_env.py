"""Create a local demo configuration exclusively; never replace an existing file."""
from pathlib import Path
import argparse


DEMO_ENV = """# Local offline demo. Use a DIFFERENT BotFather bot for Telegram development.
APP_MODE=demo
BOT_TOKEN=
ADMIN_IDS=
BOT_NAME=DriveMods development
BOT_INSTANCE_LABEL=Development notebook
DATA_DIR=data/dev
STORE_UPLOADS_ENABLED=false
STORE_PURCHASES_ENABLED=false
MBANK_QR_ENABLED=false
"""


def create(target: Path) -> None:
    # Exclusive creation also protects against a race after an existence check.
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(DEMO_ENV)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        create(args.output)
    except FileExistsError:
        parser.exit(1, "Configuration already exists; nothing changed.\n")
    print("Created offline demo configuration; no production credentials copied.")


if __name__ == "__main__":
    main()
