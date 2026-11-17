"""Download the pinned, offline Chinese/English punctuation model once."""
import hashlib
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
NAME = "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8"
URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/{NAME}.tar.bz2"
ARCHIVE_SHA256 = "c0d5aa5f8eeb686032345e180bedf39319dc2e0556781c6264bcadba8328a6e1"
MODEL_SHA256 = "65a3fb9f5ad7bfb96bf69e0dc4481df97f6ee60513c1d94ce981ba6effd524b1"


def main():
    target = ROOT / "models/punctuation/model.int8.onnx"
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == MODEL_SHA256:
        print("Offline punctuation model is ready.")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="download-", dir=target.parent) as directory:
        archive = Path(directory) / "model.tar.bz2"
        print("Downloading the punctuation model (~62 MiB)...")
        with urlopen(URL, timeout=60) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
            raise ValueError("Downloaded archive checksum mismatch")
        # Read one named member; never extract arbitrary paths from the archive.
        with tarfile.open(archive, "r:bz2") as package:
            model = package.extractfile(f"{NAME}/model.int8.onnx").read()
        if hashlib.sha256(model).hexdigest() != MODEL_SHA256:
            raise ValueError("Model checksum mismatch")
        temporary = Path(directory) / "model.int8.onnx"
        temporary.write_bytes(model)
        temporary.replace(target)
    print("Offline punctuation model is ready. Restart ReCue after updating dependencies.")


if __name__ == "__main__":
    main()
