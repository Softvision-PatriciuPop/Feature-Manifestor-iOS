import argparse
import hashlib
import sys
import datetime
from collections import defaultdict
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


ACTION_LABELS = {
    DiffEnum.ADD: "NEW VALUE",
    DiffEnum.REMOVE: "REMOVED VALUE",
    DiffEnum.CHANGE: "CHANGED VALUE",
}

# Login GitHub uses for issues created with the default GITHUB_TOKEN in Actions.
BOT_LOGIN = "github-actions[bot]"

# GitHub's hard limit for an issue body is 65536 chars; leave some headroom.
MAX_BODY_LENGTH = 65000

SNAPSHOT_DIRECTORY = "Manifest_Snapshots"


def yaml_as_dict(my_file):
    my_dict = {}
    with open(my_file, "r") as fp:
        docs = yaml.safe_load_all(fp)
        for doc in docs:
            for key, value in doc.items():
                my_dict[key] = value
    return my_dict


def fc_name_from_path(path):
    """root['feature-name']['variables']['x'] -> feature-name"""
    return path.split("root['")[1].split("']")[0]


def format_change(action, path, change):
    """Turn a single DeepDiff entry into a markdown line for the issue body."""
    label = ACTION_LABELS.get(action, action.upper())
    if isinstance(change, dict) and "new_value" in change:
        return (
            f"- **{label}** `{path}`\n"
            f"  - New: `{change['new_value']}`\n"
            f"  - Old: `{change.get('old_value')}`"
        )
    if change is not None:
        return f"- **{label}** `{path}`: `{change}`"
    return f"- **{label}** `{path}`"


def group_by_feature(ddiff):
    """Return {feature_config: [(action, markdown_line), ...]} for the whole diff."""
    grouped = defaultdict(list)
    for action, items in ddiff.items():
        # values_changed etc. are dicts (path -> change);
        # dictionary_item_added/removed are sets of paths.
        if isinstance(items, dict):
            pairs = items.items()
        else:
            pairs = ((path, None) for path in items)
        for path, change in pairs:
            grouped[fc_name_from_path(path)].append(
                (action, format_change(action, path, change))
            )
    return grouped


def find_open_auto_issues(repo):
    """
    One paginated API call: map milestone title (= feature config name) to the
    open issues the bot created for it, oldest first. Manually logged
    "Feature Tested" issues are ignored because they aren't authored by the bot.
    """
    by_fc = defaultdict(list)
    for issue in repo.get_issues(state="open"):
        if issue.pull_request is not None or issue.milestone is None:
            continue
        if issue.user.login != BOT_LOGIN:
            continue
        by_fc[issue.milestone.title].append(issue)
    for issues in by_fc.values():
        issues.sort(key=lambda i: i.number)
    return by_fc


def title_for(fc, entries):
    actions = {action for action, _ in entries}
    if len(actions) == 1:
        action = next(iter(actions))
        prefix = ACTION_LABELS.get(action, action.upper())
    else:
        prefix = "UPDATED VALUES"
    return f"{prefix} - {fc}"


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
        repo = g.get_repo(os.environ.get("REPO_NAME"))
        milestones = repo.get_milestones()
        new_milestones = all_fcs - set([i.title for i in milestones])
        for m in new_milestones:
            print(repo.create_milestone(title=m))
        milestones = repo.get_milestones()
        formatted_milestones = {i.title: i for i in milestones}

        open_issues = find_open_auto_issues(repo)
        grouped = group_by_feature(ddiff)

        for fc, entries in grouped.items():
            lines = [line for _, line in entries]
            existing = open_issues.get(fc, [])
            target = existing[0] if existing else None

            if len(existing) > 1:
                others = ", ".join(f"#{i.number}" for i in existing[1:])
                print(
                    f"WARNING: {fc} has {len(existing)} open issues; "
                    f"updating #{target.number}. Consider closing {others}."
                )

            if target is not None:
                body = target.body or ""
                new_lines = [line for line in lines if line not in body]
                if not new_lines:
                    print(f"#{target.number} ({fc}) already contains these changes, skipping")
                    continue
                update = f"\n\n### Update {current_date}\n" + "\n".join(new_lines)
                if len(body) + len(update) <= MAX_BODY_LENGTH:
                    target.edit(body=body + update)
                    print(f"Updated #{target.number} - {target.title}")
                    continue
                print(f"#{target.number} body would exceed size limit, opening a new issue")
                lines = new_lines

            issue = repo.create_issue(
                title=title_for(fc, entries),
                body=f"### {current_date}\n" + "\n".join(lines),
                milestone=formatted_milestones[fc],
            )
            print(f"Created #{issue.number} - {issue.title}")