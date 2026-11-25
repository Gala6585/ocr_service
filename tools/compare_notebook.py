"""Сверить выдачу сервиса и исходного ноутбука на одном PDF с реальным EasyOCR."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from service.tasks import reader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notebook", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    notebook = json.loads(args.notebook.read_text(encoding="utf-8"))
    module = {}
    for i in (4, 6, 8, 10, 12, 14, 16):
        exec(compile("".join(notebook["cells"][i]["source"]), f"notebook-cell-{i}", "exec"), module)
    with TemporaryDirectory() as scratch:
        result = module["run_pipeline"](
            args.pdf, 1, 1, "31.12.2030", "Учебная", output_root=scratch, reader=reader()
        )
        matches = json.loads((Path(result["run_dir"]) / "matches.json").read_text(encoding="utf-8"))
        expected = [
            (c["page"], c["bbox_crop_xyxy"], m["fio"], m["output_file"])
            for c, m in zip(result["cards"], matches)
        ]
    published = json.loads(args.results.read_text(encoding="utf-8"))
    actual = [(c["page_number"], c["bbox"], c["fio"], c["output_file"]) for c in published["cards"]]
    assert actual == expected, (actual, expected)
    print(f"Notebook parity verified for {len(actual)} cards")


if __name__ == "__main__":
    main()
