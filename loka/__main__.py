"""Command-line interface: ``python -m loka <file-or-folder> [-o OUT]``."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .decompiler import DecompileOptions, MdlDecompiler
from .compile_game import is_compile_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Loka",
        description=(
            "Decompile Source engine .mdl models (MDL v44-49, 54-59) into QC/SMD/"
            "VTA/VRD."
        ),
    )
    parser.add_argument("input", help="an .mdl file, or a folder of .mdl files")
    parser.add_argument(
        "-o", "--output", default=None, help="output folder (default: <input directory>/dec)"
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true", help="recurse into subfolders"
    )
    parser.add_argument(
        "--no-qc", action="store_true", help="skip writing the .qc file"
    )
    parser.add_argument(
        "--no-lod", action="store_true", help="skip writing LOD mesh SMDs"
    )
    parser.add_argument(
        "--no-physics", action="store_true", help="skip writing the physics SMD"
    )
    parser.add_argument(
        "--override-version", type=int, default=None, metavar="N",
        help="treat the model as MDL version N instead of reading it from the file",
    )
    parser.add_argument(
        "--stricter-format", action="store_true",
        help="omit the '// Created by' header comment and use stricter indentation",
    )
    parser.add_argument("--raw-port", action="store_true",
                        help="disable L4D2 viewmodel library repair and include packaging")
    parser.add_argument(
        "--ik-handling", choices=("default", "delete", "usesource"),
        default="default", metavar="MODE",
        help="how to write ikrule options in $animation/$sequence blocks: "
        "default (preserve IK rules), delete (drop all ikrule lines), "
        "usesource (rewrite touch ikrules to 'ikrule \"<chain>\" usesource')",
    )
    parser.add_argument(
        "--subtract-correction", action="store_true",
        help="reuse a looping idle animation as the delta sequences' subtract "
        "base (adds the idle's frame-0 pose back into the animation SMDs) "
        "instead of writing empty *_corrective_animation targets",
    )
    parser.add_argument(
        "--subtract-idle", default="", metavar="NAME",
        help="use the named idle animation as the subtract base for every delta "
        "sequence (default: automatic 'ads'/'plain' name matching per model)",
    )
    parser.add_argument(
        "--bodygroup-events", action="store_true",
        help="add AE_CL_BODYGROUP_SET_VALUE events to the ACT_VM/ACT_PRIMARY "
        "MIXED_ON/MIXED_OFF sequences, setting the mixed_* bodygroup entries "
        "(MIXED_ON -> last frame, MIXED_OFF -> frame 0)",
    )
    parser.add_argument(
        "--fix-melee-fadein", action="store_true",
        help="force fadein 0.2 on every ACT_VM_MELEE_LAYER sequence in the "
        "generated qc (a qc may contain several such sequences)",
    )
    parser.add_argument(
        "--one-click", action="store_true",
        help="one-click pipeline: back up the original model into <mdl dir>/source, "
        "decompile (subtract correction + bodygroup events) into <mdl dir>/dec, "
        "then compile with studiomdl and move the result back next to the original",
    )
    parser.add_argument(
        "--studiomdl", default="", metavar="PATH",
        help="path to a compatible model compiler; "
        "used by --one-click",
    )
    parser.add_argument(
        "--game", default="", metavar="PATH",
        help="game installation folder; used by --one-click to locate "
        "studiomdl.exe and the -game folder for compiling",
    )
    parser.add_argument("--version", action="version", version=f"Loka {__version__}")
    return parser


def collect_targets(input_path: str, recursive: bool, output_path: str = "") -> list[str]:
    if os.path.isfile(input_path):
        return [input_path]

    targets: list[str] = []
    excluded = os.path.normcase(os.path.realpath(output_path)) if output_path else ""
    if excluded == os.path.normcase(os.path.realpath(input_path)):
        excluded = ""
    if recursive:
        for root, _dirs, names in os.walk(input_path):
            _dirs[:] = [d for d in _dirs if d.lower() not in ("source", "dec")
                        and not d.startswith(".loka_compile_")
                        and os.path.normcase(os.path.realpath(os.path.join(root, d))) != excluded
                        and not is_compile_bundle(os.path.join(root, d))]
            for name in sorted(names):
                if name.lower().endswith(".mdl"):
                    targets.append(os.path.join(root, name))
    else:
        for name in sorted(os.listdir(input_path)):
            full = os.path.join(input_path, name)
            if os.path.isfile(full) and name.lower().endswith(".mdl"):
                targets.append(full)
    return targets


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    input_path = os.path.abspath(args.input)
    if not os.path.exists(input_path):
        print(f"error: not found: {input_path}", file=sys.stderr)
        return 2

    targets = collect_targets(input_path, args.recursive, args.output or "")
    if not targets:
        print("error: no .mdl files found", file=sys.stderr)
        return 2

    options = DecompileOptions(
        write_qc_file=not args.no_qc,
        write_lod_mesh_smd_files=not args.no_lod,
        write_physics_mesh_smd_file=not args.no_physics,
        override_mdl_version=args.override_version,
        stricter_format=args.stricter_format,
        ik_rule_handling=args.ik_handling,
        subtract_correction=args.subtract_correction,
        subtract_correction_idle=args.subtract_idle,
        bodygroup_events=args.bodygroup_events,
        melee_layer_fadein_fix=args.fix_melee_fadein,
        handle_vmodel_includes=not args.raw_port,
        game_path=args.game,
    )
    if args.one_click:
        options.subtract_correction = True
        options.bodygroup_events = True
        if not args.studiomdl and not args.game:
            print("error: --one-click requires --studiomdl or --game")
            return 2

    succeeded = 0
    failed = 0
    root = os.path.commonpath([os.path.dirname(os.path.abspath(p)) for p in targets])

    for index, mdl_path in enumerate(targets, start=1):
        if args.one_click:
            from .one_click import run_one_click

            print(f"[{index}/{len(targets)}] {os.path.basename(mdl_path)}")
            outcome = run_one_click(
                mdl_path,
                options,
                studiomdl_path=args.studiomdl,
                game_path=args.game,
                output_path=os.path.join(os.path.dirname(mdl_path), "dec", os.path.splitext(os.path.basename(mdl_path))[0]) if len(targets) > 1 else None,
                log=lambda message: print("    " + message),
            )
            if outcome.succeeded:
                succeeded += 1
            else:
                failed += 1
            continue

        if args.output:
            relative = os.path.relpath(os.path.dirname(mdl_path), root)
            output_path = args.output if relative in (".", "") else os.path.join(args.output, relative)
        else:
            output_path = os.path.join(os.path.dirname(os.path.abspath(mdl_path)), "dec")
        if len(targets) > 1:
            output_path = os.path.join(output_path, os.path.splitext(os.path.basename(mdl_path))[0])

        print(f"[{index}/{len(targets)}] {os.path.basename(mdl_path)}")
        result = MdlDecompiler(options).decompile(
            mdl_path, output_path, progress=lambda message: print("    " + message)
        )
        for warning in result.warnings:
            print(f"    warning: {warning}")
        for error in result.errors:
            print(f"    error: {error}")
        if result.success:
            succeeded += 1
            if args.fix_melee_fadein:
                from loka.one_click import _fix_melee_layer_fadein

                for written in result.written_files:
                    if written.lower().endswith(".qc"):
                        _fix_melee_layer_fadein(written, lambda m: print("    " + m))
            print(f"    wrote {len(result.written_files)} file(s) to {output_path}")
        else:
            failed += 1

    print(f"\n{succeeded} succeeded, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    from .process_limits import run_with_process_memory_limit

    raise SystemExit(run_with_process_memory_limit(main))
