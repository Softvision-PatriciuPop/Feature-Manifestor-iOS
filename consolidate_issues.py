#!/usr/bin/env python3
"""
One-off cleanup: consolidate duplicate auto-generated issues per feature config.

For every open issue created by github-actions[bot], grouped by milestone
(= feature config name), the OLDEST issue is kept and the others are closed as
duplicates of it.

The kept issue's description is rebuilt from the Manifest_Snapshots history:
for each date an issue was opened, the snapshot from that date is diffed
against the previous snapshot, which reproduces exactly what that run saw.
That also fixes the CHANGED VALUE descriptions, which the old script often
filled with another feature config's values. If a snapshot pair can't be
found, the original description is kept and marked for review.

The rebuilt body uses the same format as the new manifestor.py, so the next
diff run appends to it cleanly.

Usage (dry run is the default and changes nothing):
    export GITHUB_TOKEN=<token with Issues read/write + Contents read>
    python consolidate_issues.py                       # dry run, all 3 repos
    python consolidate_issues.py --show-body           # also print new bodies
    python consolidate_issues.py --only-fc urlbar      # limit to one feature
    python consolidate_issues.py --apply               # actually do it

Run it BEFORE deploying the new manifestor.py, so no new-format issues exist
yet. Re-running after a partial failure is safe: already-consolidated issues
are recognised by a hidden marker and only the remaining closes are done.
"""

import argparse
import base64
import datetime
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

import yaml
from deepdiff import DeepDiff
from github import Auth, Github, GithubException

DEFAULT_REPOS = [
    "Softvision-PatriciuPop/Feature-Manifestor-Fenix",
    "Softvision-PatriciuPop/Feature-Manifestor-Desktop",
    "Softvision-PatriciuPop/Feature-Manifestor-iOS",
]

BOT_LOGIN = "github-actions[bot]"
SNAPSHOT_DIRECTORY = "Manifest_Snapshots"
SNAPSHOT_RE = re.compile(r"^Manifest-(\d{4}-\d{2}-\d{2})\.yaml$")
MARKER_RE = re.compile(r"<!-- consolidated-from: ([\d,]+) -->")
MAX_BODY_LENGTH = 65000          # GitHub hard limit is 65536
ORIGINAL_PREVIEW_CHARS = 1500    # per issue, in the collapsed "original" block
WRITE_DELAY_SECONDS = 1.0        # stay clear of GitHub's secondary rate limits

ADD = "dictionary_item_added"
REMOVE = "dictionary_item_removed"
CHANGE = "values_changed"

# Must stay identical to ACTION_LABELS / format_change in manifestor.py so the
# new script's "already in body" check recognises these lines.
ACTION_LABELS = {
    ADD: "NEW VALUE",
    REMOVE: "REMOVED VALUE",
    CHANGE: "CHANGED VALUE",
}
CATEGORY_LABELS = {"add": "NEW VALUE", "remove": "REMOVED VALUE", "change": "CHANGED VALUE"}
TITLE_PREFIX_TO_CATEGORY = {label: cat for cat, label in CATEGORY_LABELS.items()}


# --------------------------------------------------------------------------- #
# Helpers shared with manifestor.py
# --------------------------------------------------------------------------- #

def fc_name_from_path(path):
    return path.split("root['")[1].split("']")[0]


def format_change(action, path, change):
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


def yaml_dict_from_text(text):
    result = {}
    for doc in yaml.safe_load_all(text):
        if doc:
            result.update(doc)
    return result


def category_for_action(action):
    if action == ADD:
        return "add"
    if action == REMOVE:
        return "remove"
    return "change"  # the old script titled every other action "CHANGED VALUE"


def category_for_title(title):
    prefix = title.split(" - ", 1)[0].strip()
    return TITLE_PREFIX_TO_CATEGORY.get(prefix)


def title_for(fc, categories):
    if len(categories) == 1:
        return f"{CATEGORY_LABELS[next(iter(categories))]} - {fc}"
    return f"UPDATED VALUES - {fc}"


# --------------------------------------------------------------------------- #
# Snapshot access through the API (no checkout needed)
# --------------------------------------------------------------------------- #

class SnapshotStore:
    def __init__(self, repo):
        self.repo = repo
        self._index = None
        self._docs = {}
        self._diffs = {}

    def _files(self):
        if self._index is None:
            self._index = {}
            try:
                for item in self.repo.get_contents(SNAPSHOT_DIRECTORY):
                    match = SNAPSHOT_RE.match(item.name)
                    if match:
                        self._index[match.group(1)] = item.sha
            except GithubException as exc:
                print(f"  ! could not list {SNAPSHOT_DIRECTORY}: {exc}")
        return self._index

    def _load(self, date):
        if date not in self._docs:
            # Git blobs work for files over 1 MB, unlike get_contents().
            blob = self.repo.get_git_blob(self._files()[date])
            text = base64.b64decode(blob.content).decode("utf-8")
            self._docs[date] = yaml_dict_from_text(text)
        return self._docs[date]

    def diff_for(self, date):
        """The DeepDiff the run on `date` produced, or None if snapshots are missing."""
        if date not in self._diffs:
            files = self._files()
            result = None
            if date in files:
                earlier = sorted(d for d in files if d < date)
                if earlier:
                    result = DeepDiff(
                        self._load(earlier[-1]), self._load(date), ignore_order=True
                    )
            self._diffs[date] = result
        return self._diffs[date]


def entries_for(ddiff, fc):
    """[(category, path, markdown_line)] for one feature config in a diff."""
    entries = []
    for action, items in ddiff.items():
        pairs = items.items() if isinstance(items, dict) else ((p, None) for p in items)
        for path, change in pairs:
            try:
                if fc_name_from_path(path) != fc:
                    continue
            except IndexError:
                continue
            entries.append((category_for_action(action), path, format_change(action, path, change)))
    return entries


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

@dataclass
class GroupPlan:
    fc: str
    target: object
    duplicates: list
    new_title: str = None   # None = leave title/body untouched (close-only)
    new_body: str = None
    notes: list = field(default_factory=list)


def fallback_block(issue):
    category = category_for_title(issue.title) or "change"
    lines = [f"- **{CATEGORY_LABELS[category]}** (original description from #{issue.number})"]
    for line in (issue.body or "").strip().splitlines():
        lines.append(f"  > {line}")
    if category == "change":
        lines.append(
            "  > ⚠️ The old script could attach another feature config's values to "
            "CHANGED VALUE issues; verify this against the snapshots."
        )
    return "\n".join(lines)


def build_day_section(fc, date, day_issues, store):
    """Return (lines, notes, categories) for the issues opened on one date."""
    lines, notes, categories = [], [], set()
    open_by_cat, unknown = defaultdict(list), []
    for issue in day_issues:
        cat = category_for_title(issue.title)
        (open_by_cat[cat] if cat else unknown).append(issue)

    ddiff = store.diff_for(date)
    if ddiff is None:
        notes.append(f"{date}: no snapshot pair found, kept original descriptions")
        for issue in day_issues:
            lines.append(fallback_block(issue))
            categories.add(category_for_title(issue.title) or "change")
        return lines, notes, categories

    candidates_by_cat = defaultdict(list)
    for cat, path, line in entries_for(ddiff, fc):
        candidates_by_cat[cat].append((path, line))

    for cat, open_issues in open_by_cat.items():
        categories.add(cat)
        label = CATEGORY_LABELS[cat]
        candidates = candidates_by_cat.get(cat, [])

        # NEW/REMOVED bodies are the path itself, so match exactly. This also
        # drops entries whose issue was already tested and closed.
        if cat in ("add", "remove"):
            open_paths = {(i.body or "").strip() for i in open_issues}
            matched = [line for path, line in candidates if path in open_paths]
            if len(matched) == len(open_issues):
                lines.extend(matched)
                continue

        if candidates and len(candidates) == len(open_issues):
            lines.extend(line for _, line in candidates)
        elif candidates:
            msg = (
                f"{len(open_issues)} open {label} issue(s) on {date} but the snapshot "
                f"diff has {len(candidates)} {label} entries; some may already have "
                f"been tested and closed"
            )
            notes.append(f"{date}: {msg}")
            lines.extend(line for _, line in candidates)
            lines.append(f"> ⚠️ {msg}. Please verify.")
        else:
            notes.append(f"{date}: no {label} entries in the snapshot diff, kept original descriptions")
            lines.extend(fallback_block(i) for i in open_issues)

    for issue in unknown:
        notes.append(f"#{issue.number} has an unrecognised title, kept its original description")
        lines.append(fallback_block(issue))
        categories.add("change")

    return lines, notes, categories


def originals_block(issues):
    parts = ["<details><summary>Original descriptions before consolidation</summary>", ""]
    for issue in issues:
        body = (issue.body or "").strip()
        if len(body) > ORIGINAL_PREVIEW_CHARS:
            body = body[:ORIGINAL_PREVIEW_CHARS] + "\n… (truncated)"
        parts += [f"**#{issue.number}: {issue.title}**", "", "````", body, "````", ""]
    parts.append("</details>")
    return "\n".join(parts)


def plan_group(fc, issues, store, today):
    issues = sorted(issues, key=lambda i: i.number)
    target, duplicates = issues[0], issues[1:]

    # Re-run after a partial apply: target already rebuilt, only finish closing.
    marker = MARKER_RE.search(target.body or "")
    if marker:
        done = {int(n) for n in marker.group(1).split(",")}
        if all(d.number in done for d in duplicates):
            return GroupPlan(fc, target, duplicates,
                             notes=["already consolidated, only closing remaining duplicates"])

    by_date = defaultdict(list)
    for issue in issues:
        by_date[issue.created_at.strftime("%Y-%m-%d")].append(issue)

    sections, notes, categories = [], [], set()
    for date in sorted(by_date):
        lines, day_notes, day_cats = build_day_section(fc, date, by_date[date], store)
        sections.append((date, lines))
        notes += day_notes
        categories |= day_cats

    body_parts = []
    for idx, (date, lines) in enumerate(sections):
        header = f"### {date}" if idx == 0 else f"### Update {date}"
        body_parts.append(header + "\n" + "\n".join(lines))
    body = "\n\n".join(body_parts)

    numbers = ",".join(str(i.number) for i in issues)
    if duplicates:
        footer = (f"\n\n---\n_Consolidated from "
                  f"{', '.join('#' + str(i.number) for i in issues)} on {today}._")
    else:
        footer = f"\n\n---\n_Description rebuilt from snapshots on {today}._"
    footer += f"\n<!-- consolidated-from: {numbers} -->"
    details = "\n\n" + originals_block(issues)

    if len(body) + len(footer) + len(details) <= MAX_BODY_LENGTH:
        body += footer + details
    elif len(body) + len(footer) <= MAX_BODY_LENGTH:
        body += footer
        notes.append("original descriptions omitted to stay under GitHub's size limit")
    else:
        notes.append("SKIPPED: consolidated body would exceed GitHub's size limit")
        return None if not duplicates else GroupPlan(fc, target, [], notes=notes)

    return GroupPlan(fc, target, duplicates, title_for(fc, categories), body, notes)


def collect_groups(repo):
    groups = defaultdict(list)
    for issue in repo.get_issues(state="open"):
        if issue.pull_request is not None or issue.milestone is None:
            continue
        if issue.user.login != BOT_LOGIN:
            continue
        groups[issue.milestone.title].append(issue)
    return groups


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #

def close_as_duplicate(issue):
    try:
        issue.edit(state="closed", state_reason="duplicate")
    except GithubException as exc:
        if exc.status != 422:
            raise
        # Older API/PyGithub combos don't accept "duplicate"
        issue.edit(state="closed", state_reason="not_planned")


def apply_plan(plan):
    target = plan.target
    if plan.new_body is not None:
        target.edit(title=plan.new_title, body=plan.new_body)
        time.sleep(WRITE_DELAY_SECONDS)
    for dup in plan.duplicates:
        dup.create_comment(
            f"Duplicate of #{target.number}\n\n"
            f"All open issues for `{plan.fc}` were consolidated into #{target.number}; "
            f"the changes from this issue are listed there."
        )
        time.sleep(WRITE_DELAY_SECONDS)
        close_as_duplicate(dup)
        time.sleep(WRITE_DELAY_SECONDS)


def describe(plan, show_body):
    target = plan.target
    keep = f"keep #{target.number}"
    if plan.duplicates:
        keep += ", close " + ", ".join(f"#{d.number}" for d in plan.duplicates)
    print(f"\n  {plan.fc}: {keep}")
    if plan.new_title and plan.new_title != target.title:
        print(f"    title: {target.title!r} -> {plan.new_title!r}")
    for dup in plan.duplicates:
        activity = []
        if dup.comments:
            activity.append(f"{dup.comments} comment(s)")
        if dup.assignees:
            activity.append("assigned to " + ", ".join(a.login for a in dup.assignees))
        if activity:
            print(f"    heads-up: #{dup.number} has {', '.join(activity)}")
    for note in plan.notes:
        print(f"    note: {note}")
    if show_body and plan.new_body:
        print("    ---- new body ----")
        for line in plan.new_body.splitlines():
            print(f"    | {line}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", action="append",
                        help="owner/name; repeatable. Defaults to the Fenix, Desktop and iOS repos")
    parser.add_argument("--apply", action="store_true",
                        help="perform the changes (default is a dry run)")
    parser.add_argument("--only-fc", action="append",
                        help="limit to these feature configs (repeatable), handy for a first test")
    parser.add_argument("--include-singles", action="store_true",
                        help="also rebuild lone CHANGED VALUE issues, whose description may be wrong")
    parser.add_argument("--show-body", action="store_true",
                        help="print the rebuilt issue bodies in the dry run output")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is not set")

    g = Github(auth=Auth.Token(token))
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    mode = "APPLY" if args.apply else "DRY RUN"
    total_groups = total_closed = 0

    for repo_name in args.repo or DEFAULT_REPOS:
        print(f"\n=== {repo_name} [{mode}] ===")
        repo = g.get_repo(repo_name)
        store = SnapshotStore(repo)
        plans = []

        for fc, issues in sorted(collect_groups(repo).items()):
            if args.only_fc and fc not in args.only_fc:
                continue
            is_single = len(issues) == 1
            if is_single:
                lone = issues[0]
                already = MARKER_RE.search(lone.body or "")
                if not args.include_singles or already or category_for_title(lone.title) != "change":
                    continue
            plan = plan_group(fc, issues, store, today)
            if plan:
                plans.append(plan)

        if not plans:
            print("  nothing to do")
            continue

        for plan in plans:
            describe(plan, args.show_body)
            if args.apply:
                try:
                    apply_plan(plan)
                    print("    done")
                except GithubException as exc:
                    print(f"    FAILED: {exc} (safe to re-run)")

        closes = sum(len(p.duplicates) for p in plans)
        total_groups += len(plans)
        total_closed += closes
        print(f"\n  {repo_name}: {len(plans)} feature config(s), {closes} issue(s) to close")

    verb = "Closed" if args.apply else "Would close"
    print(f"\n{verb} {total_closed} duplicate issue(s) across {total_groups} feature config(s).")
    if not args.apply:
        print("Dry run only, nothing was changed. Re-run with --apply to perform it.")


if __name__ == "__main__":
    main()