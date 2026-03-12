import argparse
import hashlib
import sys
import datetime
from enum import StrEnum
import pathlib
import os

import yaml
from deepdiff import DeepDiff
from rich.console import Console
from rich.table import Table
import requests
from github import Github
from github import Auth


class DiffEnum(StrEnum):
    ADD = "dictionary_item_added"
    REMOVE = "dictionary_item_removed"
    CHANGE = "values_changed"


def yaml_as_dict(my_file):
    my_dict = {}
    with open(my_file, "r") as fp:
        docs = yaml.safe_load_all(fp)
        for doc in docs:
            for key, value in doc.items():
                my_dict[key] = value
    return my_dict


SNAPSHOT_DIRECTORY = "Manifest_Snapshots"

if __name__ == "__main__":
    table = Table(title="Differences")
    table.add_column("Action", style="cyan", no_wrap=True)
    table.add_column("Changed value", style="magenta2", no_wrap=True)
    table.add_column("New value", style="green")
    table.add_column("Old value", style="red")
    table.add_column("Added value", style="blue")
    table.add_column("Removed value", style="yellow")

    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", required=True)
    parser.add_argument(
        "-m", "--milestone", action=argparse.BooleanOptionalAction, required=False
    )
    parser.add_argument(
        "-o", "--output", action=argparse.BooleanOptionalAction, required=False
    )

    current_date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    latest_manifest_file_name = f"{SNAPSHOT_DIRECTORY}/Manifest-{current_date}.yaml"
    snapshot_dir = pathlib.Path(SNAPSHOT_DIRECTORY)
    last_used_yaml = str(sorted([i for i in snapshot_dir.iterdir()], reverse=True)[0])
    args = parser.parse_args()
    response = requests.get(args.url)
    with open(latest_manifest_file_name, "w") as f:
        f.write(response.text)
    file_list = list()
    file_list.append(hashlib.md5(open(last_used_yaml, "rb").read()).hexdigest())
    file_list.append(
        hashlib.md5(open(latest_manifest_file_name, "rb").read()).hexdigest()
    )

    if len(set(file_list)) == 1:
        print("Files are identical, no differences to log")
        sys.exit(0)
    a = yaml_as_dict(last_used_yaml)  # old
    b = yaml_as_dict(latest_manifest_file_name)  # new
    ddiff = DeepDiff(a, b, ignore_order=True)
    for action, items in ddiff.items():
        if action == DiffEnum.ADD or action == DiffEnum.REMOVE:
            for item in items:
                if action == DiffEnum.ADD:
                    table.add_row("ADD", "", "", "", item, "")
                else:
                    table.add_row("DELETE", "", "", "", "", item)
        else:
            for item_changed, changes in items.items():
                if isinstance(changes, dict):
                    table.add_row(
                        "CHANGE",
                        item_changed,
                        str(changes["new_value"]),
                        str(changes["old_value"]),
                        "",
                        "",
                    )
                else:
                    table.add_row(
                        "CHANGE",
                        item_changed,
                        str(changes),
                        "",
                        "",
                        "",
                    )
    console = Console()
    console.print(table)
    if args.output:
        with open(f"diff_{current_date}.json", "w") as f:
            f.write(ddiff.to_json())
    all_fcs = [i for i, _ in b.items()]
    all_fcs.extend([i for i, _ in a.items()])
    all_fcs = set(all_fcs)

    if args.milestone:
        g = Github(auth=Auth.Token(os.environ.get("GITHUB_TOKEN")))
        # g.get_user().login
        repo = g.get_repo(os.environ.get("REPO_NAME"))
        milestones = repo.get_milestones()
        new_milestones = all_fcs - set([i.title for i in milestones])
        for m in new_milestones:
            print(repo.create_milestone(title=m))
        milestones = repo.get_milestones()
        formatted_milestones = {i.title: i for i in milestones}
        for action, items in ddiff.items():
            issue_description = None
            if action == DiffEnum.ADD:
                issue_title_prefix = "NEW VALUE"
            elif action == DiffEnum.REMOVE:
                issue_title_prefix = "REMOVED VALUE"
            else:
                issue_title_prefix = "CHANGED VALUE"
                for item_changed, changes in items.items():
                    if isinstance(changes, dict):
                        issue_description = f"New Value: {str(changes['new_value'])}\n \nOld Value: {str(changes['old_value'])}"
                    else:
                        issue_description = f"{str(changes)}"
            for item in items:
                issue_title_milestone = item.split("root['")[1].split("']")[0]
                issue_title = f"{issue_title_prefix} - {issue_title_milestone}"
                print(
                    repo.create_issue(
                        title=issue_title,
                        body=item if issue_description is None else issue_description,
                        milestone=formatted_milestones[issue_title_milestone],
                    )
                )
