import fileinput
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from sys import exit

import click
import yaml

import hcl2
import modules.gitlibs as gitlibs

# Create Tempdir and Module Cache Directories
annotations = dict()
start_dir = Path.cwd()
temp_dir = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
abspath = os.path.abspath(__file__)
dname = os.path.dirname(abspath)
MODULE_DIR = str(Path(Path.home(), ".terravision", "module_cache"))
if not os.path.exists(MODULE_DIR):
    os.makedirs(MODULE_DIR)

# List of dictionary sections to extract from TF file
EXTRACT = ["module", "output", "variable", "locals", "resource", "data"]


def find_tf_files(source: str, paths=list(), recursive=False) -> list:
    global annotations
    yaml_detected = False
    # If source is a Git address, clone to temp dir
    if (
        "github" in source or "bitbucket" in source or "gitlab" in source
    ) and source.startswith("http"):
        source_location = gitlibs.clone_files(source, temp_dir.name)
    else:
        # Source is a local folder
        source_location = source.strip()
    if recursive:
        for root, _, files in os.walk(source_location):
            for file in files:
                if file.lower().endswith(".tf") or file.lower().endswith("auto.tfvars"):
                    paths.append(os.path.join(root, file))
    else:
        files = [f for f in os.listdir(source_location)]
        click.echo(f"  Added Source Location: {source}")
        for file in files:
            if file.lower().endswith(".tf") or file.lower().endswith("auto.tfvars"):
                paths.append(os.path.join(source_location, file))
            if (
                file.lower().endswith("architecture.yml")
                or file.lower().endswith("architecture.yaml")
                and not yaml_detected
            ):
                full_filepath = Path(source_location).joinpath(file)
                with open(full_filepath, "r") as file:
                    click.echo(
                        f"  Detected architecture annotation file : {file.name} \n"
                    )
                    yaml_detected = True
                    annotations = yaml.safe_load(file)
    if len(paths) == 0:
        click.echo(
            "ERROR: No Terraform .tf files found in current directory or your source location. Use --source parameter to specify location or Github URL of source files"
        )
        exit()
    return paths


def handle_module(modules_list, tf_file_paths, filename):
    temp_modules_dir = temp_dir.name
    module_source_dict = dict()
    all_repos = list()
    # For every module source location, download the files into a new temporary subdirectory
    for i in modules_list:
        for k in i.keys():
            if isinstance(i[k]["source"], list):
                sourceURL = i[k]["source"][0]
            else:
                sourceURL = i[k]["source"]
            if not sourceURL in all_repos:
                all_repos.append(sourceURL)
                # Handle local modules on disk
                if sourceURL.startswith(".") or sourceURL.startswith("\\"):
                    if not str(temp_modules_dir) in filename:
                        current_filepath = os.path.abspath(filename)
                        tf_dir = os.path.dirname(current_filepath)
                        os.chdir(tf_dir)
                        os.chdir(sourceURL)
                        modfolder = str(os.getcwd())
                        tf_file_paths = find_tf_files(os.getcwd(), tf_file_paths)
                        os.chdir(start_dir)
                else:
                    modfolder = gitlibs.clone_files(sourceURL, temp_modules_dir, k)
                    tf_file_paths = find_tf_files(modfolder, tf_file_paths)
    # Create a mapping dict between modules and their source dirs for variable separation
    for i in range(len(modules_list)):
        module_stanza = modules_list[i]
        key = next(iter(module_stanza))  # Get first key
        module_source = module_stanza[key]["source"]
        # Convert Source URLs to module cache paths
        if not module_source.startswith(".") and not module_source.startswith("\\"):
            localfolder = module_source.replace("/", "_")
            cache_path = str(
                os.path.join(temp_modules_dir, ";" + key + ";" + localfolder)
            )
            module_source_dict[key] = {
                "cache_path": str(cache_path),
                "source_file": filename,
            }
        else:
            module_source_dict[key] = {
                "cache_path": module_source,
                "source_file": filename,
            }
    return {"tf_file_paths": tf_file_paths, "module_source_dict": module_source_dict}


def iterative_read(
    tf_file_paths: dict, hcl_dict: dict, extract_sections: list, tfdata: dict
):
    # Parse each TF file encountered in source locations
    module_source_dict = dict()
    for filename in tf_file_paths:
        filepath = Path(filename)
        fname = filepath.parent.name + "/" + filepath.name
        click.echo(f"  Parsing {filename}")
        with click.open_file(filename, "r", encoding="utf8") as f:
            # with suppress(Exception):
            hcl_dict[filename] = hcl2.load(f)
            # Handle HCL parsing errors due to unexpected characters
            if not filename in hcl_dict.keys():
                click.echo(
                    f"   WARNING: Unknown Error reading TF file {filename}. Attempting character cleanup fix.."
                )
                with tempfile.TemporaryDirectory(dir=temp_dir.name) as tempclean:
                    f_tmp = clean_file(filename, str(tempclean))
                    hcl_dict[filename] = hcl2.load(f_tmp)
                    if not filename in hcl_dict.keys():
                        click.echo(
                            f"   ERROR: Unknown Error reading TF file {filename}. Aborting!"
                        )
                        exit()
            # Isolate variables, locals and other sections of interest into tfdata dict
            for section in extract_sections:
                if section in hcl_dict[filename]:
                    section_name = "all_" + section
                    if not section_name in tfdata.keys():
                        tfdata[section_name] = {}
                    tfdata[section_name][filename] = hcl_dict[filename][section]
                    click.echo(
                        click.style(
                            f"    Found {len(hcl_dict[filename][section])} {section} stanza(s)",
                            fg="green",
                        )
                    )
                    if section == "module":
                        # Expand source locations to include any newly found sub-module locations
                        module_data = handle_module(
                            hcl_dict[filename]["module"], tf_file_paths, filename
                        )
                        tf_file_paths = module_data["tf_file_paths"]
                        # Get list of modules and their sources
                        for mod in module_data["module_source_dict"]:
                            module_source_dict[mod] = module_data["module_source_dict"][
                                mod
                            ]
                        if "module_source_dict" in tfdata.keys():
                            tfdata["module_source_dict"] = {
                                **tfdata["module_source_dict"],
                                **module_source_dict,
                            }
                        else:
                            tfdata["module_source_dict"] = module_source_dict
    return tfdata


def parse_tf_files(source_list: list, varfile_list: tuple, annotate: str):  # -> dict
    global annotations
    """ Parse all .TF extension files in source folder and returns dict with variables and resources found """
    hcl_dict = dict()
    tfdata = dict()
    variable_list = dict()
    module_sources_found = dict()
    cwd = os.getcwd()
    for source in source_list:
        # Get List of Terraform Files to parse
        tf_file_paths = find_tf_files(source)
        if annotate:
            with open(annotate, "r") as file:
                click.echo(f"  Will use architecture annotation file : {file.name} \n")
                annotations = yaml.safe_load(file)
        tfdata = iterative_read(tf_file_paths, hcl_dict, EXTRACT, tfdata)
    # Auto load any tfvars
    for file in tf_file_paths:
        if "auto.tfvars" in file:
            varfile_list = varfile_list + (file,)
    # Load in variables from user file into a master list
    if len(varfile_list) == 0 and tfdata.get("all_variable"):
        varfile_list = tfdata["all_variable"].keys()
    tfdata["varfile_list"] = varfile_list
    tfdata["tempdir"] = temp_dir
    tfdata["annotations"] = annotations
    return tfdata


def clean_file(filename: str, tempdir: str):
    filepath = str(Path(tempdir, "cleaning.tmp"))
    f_tmp = click.open_file(filepath, "w")
    with fileinput.FileInput(
        filename,
        inplace=False,
    ) as file:
        for line in file:
            if line.strip().startswith("#"):
                continue
            if (
                '", "' in line
                or ":" in line
                or "*" in line
                or "?" in line
                or "[" in line
                or '("' in line
                or "==" in line
                or "?" in line
                or "]" in line
                or ":" in line
            ):
                # if '", "' in line or ':' in line or '*' in line or '?' in line or '[' in line or '("' in line or '==' in line or '?' in line or '${' in line or ']' in line:
                if "aws_" in line and not "resource" in line:
                    array = line.split("=")
                    if len(array) > 1:
                        badstring = array[1]
                    else:
                        badstring = line
                    cleaned_string = re.sub("[^0-9a-zA-Z._]+", " ", badstring)
                    line = array[0] + ' = "' + cleaned_string + '"'
                else:
                    line = f"# {line}" + "\r"
            f_tmp.write(line)
    f_tmp = click.open_file(filepath, "r")
    return f_tmp
