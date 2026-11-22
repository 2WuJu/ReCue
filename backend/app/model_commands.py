"""Manage server-side model registrations and policy from the processing computer."""
import argparse
import json
from pathlib import Path

from app.config import ROOT, Settings
from app.repositories.database import connect, migrate
from app.repositories.queue import set_model_disabled


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="Register a local weight file")
    add.add_argument("path", type=Path)
    add.add_argument("--name", help="Use a distinct name when filenames collide")
    for action in ("check", "disable", "enable"):
        actions.add_parser(action).add_argument("name")
    args = parser.parse_args()

    settings = Settings.load()
    path = args.path.resolve() if args.action == "add" else None
    name = args.name or (path.name if path else "")
    if not name.strip() or len(name) > 255 or any(c in name for c in "/\\\r\n"):
        parser.error("Use a nonempty model name without path separators")
    if path:
        if not path.is_file():
            parser.error("The model must be an existing regular file")
        config = settings.processing_config or ROOT / "processing.local.json"
        try:
            default = Path(json.loads(config.read_text(encoding="utf-8"))["model"])
        except (OSError, ValueError, KeyError, TypeError):
            default = None
        if default:
            reserved = default if name == default.name else default.parent / name
            if (reserved == default or (reserved.match("ggml-*.bin") and reserved.is_file())) and reserved.resolve() != path:
                parser.error("This name belongs to a discovered model; choose a distinct --name")

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings.database)
    with connect(settings.database) as db, db:
        if path:
            db.execute("""INSERT INTO model_controls(name, path) VALUES (?, ?)
                          ON CONFLICT(name) DO UPDATE SET path=excluded.path,
                          revision=model_controls.revision+1""", (name, str(path)))
        elif args.action == "check":
            db.execute("""INSERT INTO model_controls(name) VALUES (?)
                          ON CONFLICT(name) DO UPDATE SET revision=model_controls.revision+1""", (name,))
        else:
            set_model_disabled(db, name, args.action == "disable")
    print(f"{args.action}: {name}")


if __name__ == "__main__":
    main()
