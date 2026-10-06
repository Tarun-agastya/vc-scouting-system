"""
Create (or re-create) the other-language twin of an existing one-pager.

    python3 templates/one_pager/translate.py templates/one_pager/data/ligaro.de.yaml
    python3 templates/one_pager/translate.py data/ligaro.de.yaml --force   # replace the English one

Reads <slug>.<lang>.yaml and writes <slug>.<other>.yaml next to it. Use it:
  * after editing the German (final) version, to bring the English one up to date;
  * when generate.py could not translate (local model was busy or down);
  * on a draft written before both languages existed (<slug>.yaml, German) —
    it is renamed to <slug>.de.yaml first, then translated.

Images, logo, website and sources are copied unchanged; claim, meta line and the
five sections are translated by the local model, and every number in the result
is checked against the source version. The twin is always `status: draft`.

Same isolation contract as generate.py (FORMAT.md §7): no pipeline imports.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import generate as gen  # noqa: E402
import i18n  # noqa: E402
import llm as llm_mod  # noqa: E402


def twin_path(src: Path, src_lang: str, dst_lang: str) -> Path:
    stem = src.name[: -len(".yaml")]
    if stem.endswith(f".{src_lang}"):
        stem = stem[: -len(src_lang) - 1]
    return src.with_name(f"{stem}.{dst_lang}.yaml")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="the YAML to translate from")
    ap.add_argument("--force", action="store_true", help="overwrite the existing twin")
    args = ap.parse_args()

    src = Path(args.src).resolve()
    if not src.exists():
        print(f"✗ {src} not found")
        return 1
    data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    src_lang = i18n.lang_of(data)
    dst_lang = i18n.other(src_lang)

    # A pre-two-language file (<slug>.yaml) becomes <slug>.de.yaml first.
    if not src.name.endswith(f".{src_lang}.yaml"):
        new_src = src.with_name(f"{src.name[:-len('.yaml')]}.{src_lang}.yaml")
        if new_src.exists():
            print(f"✗ both {src.name} and {new_src.name} exist — remove one first")
            return 1
        text = src.read_text(encoding="utf-8")
        if "lang" not in data:
            text = text.replace("\nmeta:", f"\nlang: {src_lang}\n\nmeta:", 1) if "\nmeta:" in text \
                else f"lang: {src_lang}\n" + text
        new_src.write_text(text, encoding="utf-8")
        src.unlink()
        print(f"✓ Renamed {src.name} -> {new_src.name}")
        src = new_src
        data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}

    dst = twin_path(src, src_lang, dst_lang)
    if dst.exists() and not args.force:
        print(f"✗ {dst} already exists. Re-run with --force to overwrite.")
        return 1

    unhealthy = llm_mod.health()
    if unhealthy:
        print(f"✗ {unhealthy} — nothing written")
        return 1

    print(f"• Translating {src.name} ({src_lang}) -> {dst.name} ({dst_lang}) with {llm_mod.MODEL} …")
    out, ok = gen.translate_data(data, dst_lang, src_label=f" ({src.name})")
    if not ok:
        print("✗ Translation failed (the local model returned nothing usable) — nothing written")
        return 1

    header = (
        f"# GT Hub One-Pager ({i18n.labels(dst_lang)['name']}) — translated from {src.name}.\n"
        f"# Written by templates/one_pager/translate.py. Nothing here is checked yet:\n"
        f"# every item under review.open_questions needs a person.\n"
        f"# Format and rules: templates/one_pager/FORMAT.md\n\n"
    )
    gen.write_yaml(out, dst, header)
    print(f"✓ Written ({dst_lang}): {dst}")
    for q in out["review"]["open_questions"]:
        print(f"   • {' '.join(str(q).split())[:150]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
