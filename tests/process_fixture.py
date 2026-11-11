"""Real sleeping process trees for lifecycle tests; no ASR simulation."""
from pathlib import Path
import subprocess
import sys
import time


def main():
    mode, directory = sys.argv[1:3]
    directory = Path(directory)
    if mode == "host":
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
        from app.processes import run_tool
        with (directory / "out").open("wb") as out, (directory / "err").open("wb") as err:
            run_tool([sys.executable, __file__, "root", str(directory)],
                     stdout=out, stderr=err, timeout=60)
        return
    if mode in ("root", "root-exit", "child"):
        child_mode = "leaf" if mode == "child" else "child"
        subprocess.Popen([sys.executable, __file__, child_mode, str(directory)])
    import os
    (directory / f"{mode}.pid").write_text(str(os.getpid()))
    if mode == "root-exit":
        while not (directory / "release-root").exists():
            time.sleep(0.01)
        return
    time.sleep(60)


if __name__ == "__main__":
    main()
