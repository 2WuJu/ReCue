"""Run in the separate Qwen environment under ReCue's process supervisor."""
import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("model", "aligner", "audio", "output"):
        parser.add_argument(argument, type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    for path in (args.model, args.aligner, args.audio):
        if not path.is_absolute() or not path.exists():
            parser.error(f"Expected an existing absolute local path: {path}")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if version("qwen-asr") != "0.0.6":
        raise RuntimeError("ReCue requires qwen-asr 0.0.6")

    import torch
    from qwen_asr import Qwen3ASRModel

    torch.set_num_threads(8)
    model = Qwen3ASRModel.from_pretrained(
        str(args.model), dtype=torch.float32, device_map="cpu", local_files_only=True,
        use_safetensors=True, max_inference_batch_size=1, max_new_tokens=4096,
        forced_aligner=str(args.aligner),
        forced_aligner_kwargs=dict(dtype=torch.float32, device_map="cpu",
                                   local_files_only=True, use_safetensors=True),
    )
    result, = model.transcribe(audio=str(args.audio), language="Chinese",
                               return_time_stamps=not args.probe)
    if args.probe:
        payload = {"ready": True}
    else:
        payload = dict(text=result.text, words=[
            dict(text=word.text, start_time=word.start_time, end_time=word.end_time)
            for word in (result.time_stamps or [])
        ])
    args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
