"""Local punctuation restoration with a lossless map back to ASR segments."""
import hashlib
import json
from pathlib import Path
import re
import sys

MODEL_SHA256 = "65a3fb9f5ad7bfb96bf69e0dc4481df97f6ee60513c1d94ce981ba6effd524b1"
FORMAT_VERSION = "ct-transformer-int8-article-v1"
PUNCTUATION = set("，。？！、；：,.?!;:")
TERMINAL = set("。？！!?")


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def restore_insertions(source, predicted):
    """Accept inserted punctuation only; retain source spelling, code and spaces.

    The model tokenizes English/code with extra spaces. Match content characters
    exactly, then attach predictions to source offsets. Reject rewritten output.
    """
    original = [(index, char) for index, char in enumerate(source)
                if not char.isspace() and char not in PUNCTUATION]
    content = [char for char in predicted if not char.isspace() and char not in PUNCTUATION]
    if content != [char for _, char in original]:
        raise ValueError("Punctuation model changed source text")
    protected = {i for match in re.finditer(r"[A-Za-z0-9_+#./:@\\-]+", source)
                 for i in range(match.start() + 1, match.end())}
    insertions = {}
    consumed = 0
    for char in predicted:
        if char.isspace():
            continue
        if char not in PUNCTUATION:
            consumed += 1
            continue
        if not consumed:
            continue
        offset = original[consumed - 1][0] + 1
        next_offset = original[consumed][0] if consumed < len(original) else len(source)
        # Keep existing punctuation (including code dots) exactly as transcribed.
        if offset in protected or any(c in PUNCTUATION for c in source[offset:next_offset]):
            continue
        if offset not in insertions:
            insertions[offset] = char
    return insertions


def contextual_insertions(source, punctuate):
    try:
        return restore_insertions(source, punctuate(source))
    except ValueError:
        # Some model inputs lose a repeated phrase in a long context. Re-infer
        # shorter contexts instead of accepting deletions or inventing text.
        if len(source) <= 80:
            raise
        spaces = [i for i, char in enumerate(source) if char.isspace() and 0 < i < len(source) - 1]
        cut = min(spaces, key=lambda i: abs(i - len(source) / 2)) if spaces else len(source) // 2
        while cut > 0 and source[cut - 1].isascii() and source[cut].isascii() and not source[cut].isspace():
            cut -= 1
        if not cut:
            raise
        left = contextual_insertions(source[:cut], punctuate)
        right = contextual_insertions(source[cut:], punctuate)
        return {**left, **{cut + offset: char for offset, char in right.items()}}


def format_article(segments, punctuate):
    """Run contextual chunks and return paragraph parts with UTF-16 source offsets."""
    # Context spans many ASR segments; do not punctuate isolated fragments.
    chunks, chunk, count = [], [], 0
    for index, segment in enumerate(segments):
        for start in range(0, len(segment["text"]), 1000):
            value = segment["text"][start:start + 1000]
            if chunk and count + len(value) > 1000:
                chunks.append(chunk); chunk, count = [], 0
            chunk.append((index, start, value)); count += len(value)
    if chunk:
        chunks.append(chunk)
    additions = [{} for _ in segments]
    for chunk in chunks:
        source = " ".join(value for _, _, value in chunk)
        inserted = contextual_insertions(source, punctuate)
        cursor = 0
        for index, start, value in chunk:
            for offset, punctuation in inserted.items():
                if cursor < offset <= cursor + len(value):
                    additions[index][start + offset - cursor] = punctuation
            cursor += len(value) + 1

    for index, segment in enumerate(segments[:-1]):
        text, following = segment["text"], segments[index + 1]["text"]
        if (text and following and text[-1].isascii() and text[-1].isalnum()
                and following[0].isascii() and following[0].isalnum()):
            additions[index].setdefault(len(text), " ")

    paragraphs, paragraph = [], []
    length = sentences = 0
    for index, segment in enumerate(segments):
        text = segment["text"]
        boundaries = sorted(set(additions[index]) | {len(text)} |
                            {i + 1 for i, char in enumerate(text) if char in TERMINAL})
        start = 0
        for end in boundaries:
            if end <= start:
                continue
            suffix = additions[index].get(end, "")
            value = text[start:end] + suffix
            paragraph.append({"segment": index, "start": utf16_length(text[:start]),
                              "end": utf16_length(text[:end]), "suffix": suffix})
            length += len(value)
            terminal = bool(value.strip()) and value.strip()[-1] in TERMINAL | {"."}
            if terminal:
                sentences += 1
            next_gap = (segments[index + 1]["start_ms"] - segment["end_ms"]
                        if end == len(text) and index + 1 < len(segments) else 0)
            if ((terminal and (length >= 180 or sentences >= 3 or
                               (length >= 60 and next_gap >= 1500))) or length >= 420):
                paragraphs.append(paragraph); paragraph = []; length = sentences = 0
            start = end
    if paragraph:
        paragraphs.append(paragraph)
    return paragraphs


def main():
    # Called as an isolated, timeout-controlled process, never inside the API.
    model, input_path, output_path = map(Path, sys.argv[1:])
    if hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError("Punctuation model checksum mismatch")
    import sherpa_onnx
    engine = sherpa_onnx.OfflinePunctuation(sherpa_onnx.OfflinePunctuationConfig(
        model=sherpa_onnx.OfflinePunctuationModelConfig(
            ct_transformer=str(model), num_threads=2, provider="cpu")))
    segments = json.loads(input_path.read_text(encoding="utf-8"))
    paragraphs = format_article(segments, engine.add_punctuation)
    output_path.write_text(json.dumps(paragraphs, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
