#!/usr/bin/env python3
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from functools import reduce
from operator import mul
from warnings import warn

from .algorithm import eval_sort, eval_all
from .tools import combinations
from .template import EvalBlock
from .grid_builtins import builtins
from .files import match_files, match_template_files, write_grid

parser = argparse.ArgumentParser(description="Creates an array [grid] of similar jobs and executes [submits] them")
parser.add_argument("-f", "--files", nargs="+", help="files to be processed", metavar="FILE", default=tuple())
parser.add_argument("-t", "--static", nargs="+", help="files to be copied", metavar="FILE", default=tuple())
parser.add_argument("-n", "--name", help="grid folder naming pattern", metavar="PATTERN", default="grid%d")
parser.add_argument("-r", "--recursive", help="visit sub-folders when matching file names", action="store_true")
parser.add_argument("-m", "--max", help="maximum allowed grid size", metavar="N", default=10_000)
parser.add_argument("-s", "--settings", help="setting file name", metavar="FILE", default=".grid")
parser.add_argument("-l", "--log", help="log file name", metavar="FILE", default=".grid.log")
parser.add_argument("--root", help="root folder for scanning/placing grid files", default=".")
parser.add_argument("action", help="action to perform", choices=["new", "run", "cleanup", "distribute"])
parser.add_argument("command", nargs="*", help="command to execute for 'run' action")

options = parser.parse_args()

logging.basicConfig(filename=options.log, filemode="w", level=logging.INFO)
logging.info(' '.join(sys.argv))


def get_grid_state(options):
    """Reads the grid state"""
    try:
        with open(options.settings, "r") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"Grid file does not exit: {repr(e.filename)}") from e


def save_grid_state(options, state):
    """Saves the grid state"""
    with open(options.settings, "w") as f:
        json.dump(state, f, indent=4)


def folder_name(options, index):
    """Folder name convention"""
    return options.name % index


def grid_match_static(options):
    logging.info("Matching static files")
    result = match_files(options.static, allow_empty=True, recursive=options.recursive)
    for i in result:
        logging.info(f"  {str(i)}")
    logging.info(f"Total: {len(result)} files")
    return result


def grid_match_templates(options, exclude):
    logging.info("Matching template files")
    request = []
    if options.action == "distribute":
        request = (*options.files, *options.command)
    elif options.action == "new":
        request = options.files
        if len(request) == 0:
            request = "*",
    result = match_template_files(request, recursive=options.recursive, exclude=exclude)
    for i in result:
        logging.info(f"  {str(i)}")
    logging.info(f"Total: {len(result)} files")
    return result


def grid_collect_statements(files_grid):
    statements = {}
    for grid_file in files_grid:
        for chunk in grid_file.chunks:
            if isinstance(chunk, EvalBlock):
                if chunk.name in statements:
                    raise ValueError(f"duplicate statement {chunk} (also {statements[chunk.name]}")
                else:
                    statements[chunk.name] = chunk
    return statements


def grid_group_statements(options, statements):
    statements_core = {}
    statements_dependent = {}

    for name, statement in statements.items():
        logging.info(repr(statement))
        if len(statement.names_missing(builtins)) == 0 and options.action != "distribute":
            logging.info("  core, evaluating ...")
            result = statement.eval(builtins)
            if "__len__" not in dir(result):
                result = [result]
            logging.info(f"  result: {result} (len={len(result)})")
            statements_core[name] = result

        else:
            logging.info(f"  depends on: {', '.join(map(repr, statement.required))}")
            statements_dependent[name] = statement
    total = reduce(mul, map(len, statements_core.values())) if len(statements_core) else 1
    logging.info(f"Total: {len(statements_core)} core statement(s) ({total} combination(s)), "
                 f"{len(statements_dependent)} dependent statement(s)")
    if total > options.max:
        raise RuntimeError(f"the total grid size {total} is above threshold {options.max}")
    return statements_core, statements_dependent


# ----------------------------------------------------------------------
#   New grid, distribute
# ----------------------------------------------------------------------

if options.action in ("new", "distribute"):

    # Errors
    if len(options.command) == 0 and options.action == "distribute":
        parser.error("nothing to distribute")

    # Defaults

    if not options.static:
        options.static = []

    if options.action == "distribute":
        grid_state = get_grid_state(options)
        logging.info("Continue with previous {n} instances".format(n=len(grid_state["grid"])))

    # ------------------------------------------------------------------
    #   Common part
    # ------------------------------------------------------------------

    files_static = grid_match_static(options)
    files_grid = grid_match_templates(options, files_static)
    statements = grid_collect_statements(files_grid)

    reserved_names = set(builtins) | {"__grid_folder_name__", "__grid_id__"}
    overlap = set(statements).intersection(reserved_names)
    if len(overlap) > 0:
        raise ValueError(f"the following names used in the grid are reserved: {', '.join(overlap)}")

    statements_core, statements_dependent = grid_group_statements(options, statements)
    total = 1

    # Read previous run
    if options.action == "distribute":
        overlap = set(grid_state["names"]).intersection(set(statements))
        if len(overlap) > 0:
            raise ValueError(f"new statement names overlap with previously defined ones: {', '.join(overlap)}")
    else:
        grid_state = {"grid": {}, "names": list(statements)}

    index = len(grid_state["grid"])

    # Check if folders already exist
    # TODO: this checks if there are any folders starting with [former] prefix
    # for x in glob.glob(options.prefix + "*"):
    #     if not x in grid_state["grid"]:
    #         print(
    #             "File or folder {name} may conflict with the grid. Either remove it or use a different prefix through '--prefix' option.".format(
    #                 name=x))
    #         logging.error("{name} already exists".format(name=x))
    #         sys.exit(1)

    # ------------------------------------------------------------------
    #   New
    # ------------------------------------------------------------------

    if options.action == "new":
        if len(statements_core) == 0:
            warn(f"No fixed groups found")

        # Figure out order
        ordered_statements = eval_sort(statements_dependent, reserved_names | set(statements_core))
        # Iterate over possible combinations and write a grid
        for stack in combinations(statements_core):
            scratch = folder_name(options, index)
            stack["__grid_folder_name__"] = scratch
            stack["__grid_id__"] = index

            values = eval_all(ordered_statements, {**stack, **builtins})
            stack.update({statement.name: v for statement, v in zip(ordered_statements, values)})
            grid_state["grid"][scratch] = {"stack": stack}
            logging.info(f"  composing {scratch}")
            write_grid(scratch, stack, files_static, files_grid, options.root)
            index += 1

        # Save state
        save_grid_state(options, grid_state)

    # ------------------------------------------------------------------
    #   Distribute
    # ------------------------------------------------------------------

    elif options.action == "distribute":
        assert len(statements_core) == 0
        if len(statements_dependent) == 0:
            warn("No dependent statements found. File(s) will be distributed as-is.")

        logging.info(f"Distributing files into {len(grid_state)} folders")
        exceptions = []

        # Figure out order
        ordered_statements = eval_sort(statements_dependent, set(grid_state["names"]))

        for k, v in grid_state["grid"].items():
            if not Path(k).is_dir():
                logging.exception(f"Grid folder {k} does not exist")
                exceptions.append(FileNotFoundError(f"No such file or directory: {repr(k)}"))
            else:
                stack = v["stack"]
                values = eval_all(ordered_statements, v["stack"])
                stack.update({statement.name: v for statement, v in zip(ordered_statements, values)})
                write_grid(k, stack, files_static, files_grid, options.root)
        if len(exceptions) > 0:
            raise exceptions[-1]

# ----------------------------------------------------------------------
#   Execute in context of grid
# ----------------------------------------------------------------------

elif options.action == "run":
    if len(options.command) == 0:
        parser.error("missing command to run")

    current_state = get_grid_state(options)
    logging.info(f"Executing {' '.join(options.command)} in {len(current_state['grid'])} grid folders")
    exceptions = []
    for cwd in current_state["grid"]:

        try:
            print(cwd)
            print(subprocess.check_output(options.command, cwd=cwd, stderr=subprocess.PIPE, text=True))
        except (FileNotFoundError, subprocess.SubprocessError) as e:
            print(f"Failed to execute {' '.join(options.command)} (working directory {repr(cwd)})")
            logging.exception(f"Failed to execute {' '.join(options.command)} (working directory {repr(cwd)})")
            exceptions.append(e)
    if len(exceptions) > 0:
        raise exceptions[-1]

# ----------------------------------------------------------------------
#   Cleanup grid
# ----------------------------------------------------------------------

elif options.action == "cleanup":
    current_state = get_grid_state(options)
    logging.info("Removing grid folders")
    exceptions = []
    for f in current_state["grid"]:
        try:
            shutil.rmtree(f)
            logging.info(f"  {f}")
        except Exception as e:
            exceptions.append(e)
            logging.exception(f"Error while removing {f}")
    if len(exceptions):
        logging.error(f"{len(exceptions)} exceptions occurred while removing grid folders")
    logging.info("Removing the data file")
    os.remove(options.settings)
    if len(exceptions):
        raise exceptions[-1]


def dummy():
    pass  # TODO: remove dummy function needed for console_scripts
