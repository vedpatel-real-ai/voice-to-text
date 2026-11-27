"""Prepare the public benchmark rows in a fresh scratch directory; no uploads."""
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

for dataset, config, split, count in [
    ("MikhailT/cmu-arctic", "default", "ksp", 6),
    ("MikhailT/cmu-arctic", "default", "awb", 6),
    ("openslr/librispeech_asr", "clean", "test", 5),
]:
    query = urlencode(dict(dataset=dataset, config=config, split=split, offset=0, length=count))
    with urlopen("https://datasets-server.huggingface.co/rows?" + query, timeout=30) as response:
        rows = json.load(response)["rows"]
    if len(rows) != count:
        raise RuntimeError("Unexpected dataset length")
    refs = []
    for index, entry in enumerate(rows):
        row = entry["row"]
        name = row["id"] if split == "test" else f"{split}-{index}"
        audio = row["audio"]
        src = (audio[0] if isinstance(audio, list) else audio)["src"]
        with urlopen(src, timeout=30) as response:
            Path(name + ".media").write_bytes(response.read())
        refs.append({key: value for key, value in row.items() if key != "audio"})
    if split == "test":
        Path("libri.json").write_text(json.dumps({"rows": [{"row": ref} for ref in refs]}))
    else:
        Path(split + ".json").write_text(json.dumps(refs))
