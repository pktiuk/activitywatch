#!/usr/bin/env python3
"""
Script that outputs a changelog for the repository in the current directory and its submodules.

Manual actions needed to clean up for changelog:
 - Reorder modules in a logical order (aw-webui, aw-server, aw-server-rust, aw-watcher-window, aw-watcher-afk, ...)
 - Remove duplicate aw-webui entries
"""

import shlex
import re
import argparse
import os
import logging
from time import sleep
from typing import Optional, Tuple, List, Dict
from subprocess import run as _run, STDOUT, PIPE
from dataclasses import dataclass
from collections import defaultdict
from collections.abc import Collection

import requests

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# preferred repository order
repo_order = [
    "activitywatch",
    "aw-server",
    "aw-server-rust",
    "aw-webui",
    "aw-watcher-afk",
    "aw-watcher-window",
    "aw-qt",
    "aw-core",
    "aw-client",
]


class CommitMsg:
    type: str
    subtype: str
    msg: str


@dataclass
class Commit:
    id: str
    msg: str
    repo: str

    @property
    def msg_processed(self) -> str:
        """Generates links from commit and issue references (like 0c14d77, #123) to correct repo and such"""
        s = self.msg
        s = re.sub(
            r"[^(-]https://github.com/ActivityWatch/([\-\w\d]+)/(issues|pulls)/(\d+)",
            r"[#\3](https://github.com/ActivityWatch/\1/issues/\3)",
            s,
        )
        s = re.sub(
            r"#(\d+)",
            rf"[#\1](https://github.com/ActivityWatch/{self.repo}/issues/\1)",
            s,
        )
        s = re.sub(
            r"[\s\(][0-9a-f]{7}[\s\)]",
            rf"[`\0`](https://github.com/ActivityWatch/{self.repo}/issues/\0)",
            s,
        )
        return s

    def parse_type(self) -> Optional[Tuple[str, str]]:
        # Needs to handle '!' indicating breaking change
        match = re.search(r"^(\w+)(\((.+)\))?[!]?:", self.msg)
        if match:
            type = match.group(1)
            subtype = match.group(3)
            if type in ["build", "ci", "fix", "feat"]:
                return type, subtype
        return None

    @property
    def type(self) -> Optional[str]:
        _type, _ = self.parse_type() or (None, None)
        return _type

    @property
    def subtype(self) -> Optional[str]:
        _, subtype = self.parse_type() or (None, None)
        return subtype

    def type_str(self) -> str:
        _type, subtype = self.parse_type() or (None, None)
        return f"{_type}" + (f"({subtype})" if subtype else "")

    def format(self) -> str:
        commit_link = commit_linkify(self.id, self.repo) if self.id else ""

        return f"{self.msg_processed}" + (f" ({commit_link})" if commit_link else "")


def run(cmd, cwd=".") -> str:
    logger.debug(f"Running in {cwd}: {cmd}")
    p = _run(shlex.split(cmd), stdout=PIPE, stderr=STDOUT, encoding="utf8", cwd=cwd)
    if p.returncode != 0:
        print(p.stdout)
        print(p.stderr)
        raise Exception
    return p.stdout


def pr_linkify(prid: str, repo: str) -> str:
    return f"[#{prid}](https://github.com/ActivityWatch/{repo}/pulls/{prid})"


def commit_linkify(commitid: str, repo: str) -> str:
    return f"[`{commitid}`](https://github.com/ActivityWatch/{repo}/commit/{commitid})"


def wrap_details(title, body, wraplines=5):
    """Wrap lines into a <details> element if body is longer than `wraplines`"""
    out = f"\n\n### {title}"
    if body.count("\n") > wraplines:
        out += "\n<details><summary>Click to expand</summary>"
    out += f"\n<p>\n\n{body.rstrip()}\n\n</p>\n"
    if body.count("\n") > wraplines:
        out += "</details>"
    return out


contributor_emails = set()


def summary_repo(path: str, commitrange: str, filter_types: List[str]) -> str:
    if commitrange.endswith("0000000"):
        # Happens when a submodule has been removed
        return ""
    if commitrange.startswith("0000000"):
        # Happens when a submodule has been added
        commitrange = ""  # no range = all commits

    dirname = run("bash -c 'basename $(pwd)'", cwd=path).strip()
    out = f"\n## 📦 {dirname}"

    feats = ""
    fixes = ""
    misc = ""

    # pretty format is modified version of: https://stackoverflow.com/a/1441062/965332
    summary_bundle = run(
        f"git log {commitrange} --no-decorate --pretty=format:'%h%x09%an%x09%ae%x09%s'",
        cwd=path,
    )
    for line in summary_bundle.split("\n"):
        if line:
            _id, _author, email, msg = line.split("\t")
            # will add author email to contributor list
            # the `contributor_emails` is global and collected later
            contributor_emails.add(email)
            commit = Commit(
                id=_id,
                msg=msg,
                repo=dirname,
            )

            entry = f"\n - {commit.format()}"
            if commit.type == "feat":
                feats += entry
            elif commit.type == "fix":
                fixes += entry
            elif commit.type not in filter_types:
                misc += entry

    for name, entries in (("✨ Features", feats), ("🐛 Fixes", fixes), ("🔨 Misc", misc)):
        if entries:
            _count = len(entries.strip().split("\n"))
            title = f"{name} ({_count})"
            if "Misc" in name or "Fixes" in name:
                out += wrap_details(title, entries)
            else:
                out += f"\n\n### {title}"
                out += entries

    # NOTE: For now, these TODOs can be manually fixed for each changelog.
    # TODO: Fix issue where subsubmodules can appear twice (like aw-webui)
    # TODO: Use specific order (aw-webui should be one of the first, for example)
    summary_subrepos = run(
        f"git submodule summary {commitrange.split('...')[0]}", cwd=path
    )
    subrepos = {}
    for header, *_ in [s.split("\n") for s in summary_subrepos.split("\n\n")]:
        if header.startswith("fatal: not a git repository"):
            # Happens when a submodule has been removed
            continue
        if header.strip():
            if len(header.split(" ")) < 4:
                # Submodule may have been deleted
                continue

            _, name, commitrange, count = header.split(" ")
            count = count.strip().lstrip("(").rstrip("):")
            logger.info(
                f"Found {name}, looking up range: {commitrange} ({count} commits)"
            )
            name = name.strip(".").strip("/")

            subrepos[name] = summary_repo(
                f"{path}/{name}", commitrange, filter_types=filter_types
            )

    # pick subrepos in repo_order, and remove from dict
    for name in repo_order:
        if name in subrepos:
            out += "\n"
            out += subrepos[name]
            logger.info(f"{name:12} length: \t{len(subrepos[name])}")
            del subrepos[name]

    # add remaining repos
    for name, output in subrepos.items():
        out += "\n"
        out += output

    return out


# FIXME: Doesn't work, messy af, just gonna have to remove the aw-webui section by hand
def remove_duplicates(s: List[str], minlen=10, only_sections=True) -> List[str]:
    """
    Removes the longest sequence of repeated elements (they don't have to be adjacent), if sequence if longer than `minlen`.
    Preserves order of elements.
    """
    if len(s) < minlen:
        return s
    out = []
    longest: List[str] = []
    for i in range(len(s)):
        if i == 0 or s[i] not in out:
            # Not matching any previous line,
            # so add longest and new line to output, and reset longest
            if len(longest) < minlen:
                out.extend(longest)
            else:
                duplicate = "\n".join(longest)
                print(f"Removing duplicate '{duplicate[:80]}...'")
            out.append(s[i])
            longest = []
        else:
            # Matches a previous line, so add to longest
            # If longest is empty and only_sections is True, check that the line is a section start
            if only_sections:
                if not longest and s[i].startswith("#"):
                    longest.append(s[i])
                else:
                    out.append(s[i])
            else:
                longest.append(s[i])

    return out


def build(filter_types=["build", "ci", "tests", "test"]):
    prev_release = run("git describe --tags --abbrev=0").strip()
    next_release = "master"

    parser = argparse.ArgumentParser(description="Generate changelog from git history")
    parser.add_argument(
        "--range", default=f"{prev_release}...{next_release}", help="Git commit range"
    )
    parser.add_argument("--path", default=".", help="Path to git repo")
    parser.add_argument(
        "--output", default="changelog.md", help="Path to output changelog"
    )
    args = parser.parse_args()

    since, until = args.range.split("...")
    tag = until

    # provides a commit summary for the repo and subrepos, recursively looking up subrepos
    # NOTE: this must be done *before* `get_all_contributors` is called,
    #       as the latter relies on summary_repo looking up all users and storing in a global.
    logger.info("Generating commit summary")
    output_changelog = summary_repo(
        ".", commitrange=args.range, filter_types=filter_types
    )

    output_changelog = f"""
# Changelog

Changes since {since}

{output_changelog}
    """.strip()

    # Would ideally sort by number of commits or something, but that's tricky
    usernames = sorted(get_all_contributors(), key=str.casefold)
    twitter_handles = get_twitter_of_ghusers(usernames)
    print(", ".join("@" + handle for handle in twitter_handles.values() if handle))

    output_contributors = f"""# Contributors

Thanks to everyone who contributed to this release:

{', '.join(('@' + username for username in usernames))}"""

    # Header starts here
    logger.info("Building final output")
    output = f"""# {tag}"""
    output += "\n\n"
    output += f"These are the release notes for ActivityWatch version {tag}.".strip()
    output += "\n\n"
    output += "**New to ActivityWatch?** Check out the [website](https://activitywatch.net) and the [README](https://github.com/ActivityWatch/activitywatch/blob/master/README.md)."
    output += "\n\n"
    output += """# Installation

See the [getting started guide in the documentation](https://docs.activitywatch.net/en/latest/getting-started.html).
    """.strip()
    output += "\n\n"
    output += f"""# Downloads

 - [**Windows**](https://github.com/ActivityWatch/activitywatch/releases/download/{tag}/activitywatch-{tag}-windows-x86_64-setup.exe) (.exe, installer)
 - [**macOS**](https://github.com/ActivityWatch/activitywatch/releases/download/{tag}/activitywatch-{tag}-macos-x86_64.dmg) (.dmg)
 - [**Linux**](https://github.com/ActivityWatch/activitywatch/releases/download/{tag}/activitywatch-{tag}-linux-x86_64.zip) (.zip)
 """.strip()
    output += "\n\n"
    output += output_contributors.strip() + "\n\n"
    output += output_changelog.strip() + "\n\n"

    output = output.replace("# activitywatch", "# activitywatch (bundle repo)")
    with open(args.output, "w") as f:
        f.write(output)
    print(f"Wrote {len(output.splitlines())} lines to {args.output}")


def _resolve_email(email: str) -> Optional[str]:
    if "users.noreply.github.com" in email:
        username = email.split("@")[0]
        if "+" in username:
            username = username.split("+")[1]
        # TODO: Verify username is valid using the GitHub API
        print(f"Contributor: @{username}")
        return username
    else:
        resp = None
        backoff = 0
        max_backoff = 2
        while resp is None:
            if backoff >= max_backoff:
                logger.warning(f"Backed off {max_backoff} times, giving up")
                break
            try:
                logger.info(f"Sending request for {email}")
                _resp = requests.get(
                    f"https://api.github.com/search/users?q={email}+in%3Aemail"
                )
                _resp.raise_for_status()
                resp = _resp
                backoff = 0
            # if rate limit exceeded, back off
            except requests.exceptions.RequestException as e:
                if isinstance(e, requests.exceptions.HTTPError):
                    if e.response.status_code == 403:
                        logger.warning("Rate limit exceeded, backing off...")
                        backoff += 1
                        sleep(3)
                        continue
                else:
                    raise e
            finally:
                # Just to respect API limits...
                sleep(1)

        if resp:
            data = resp.json()
            if data["total_count"] == 0:
                logger.info(f"No match for email: {email}")
            if data["total_count"] > 1:
                logger.warning(f"Multiple matches for email: {email}")
            if data["total_count"] >= 1:
                username = data["items"][0]["login"]
                logger.info(f"Contributor: @{username}  (by email: {email})")
                return username
    return None


def get_all_contributors() -> set[str]:
    # TODO: Merge with contributor-stats?
    logger.info("Getting all contributors")

    # We will commit this file, to act as a cache (preventing us from querying GitHub API every time)
    filename = "scripts/changelog_contributors.csv"

    # mapping from username to one or more emails
    usernames: Dict[str, set] = defaultdict(set)

    # some hardcoded ones, some that don't resolve...
    usernames["erikbjare"] |= {"erik.bjareholt@gmail.com", "erik@bjareho.lt"}
    usernames["iloveitaly"] |= {"iloveitaly@gmail.com"}
    usernames["kewde"] |= {"kewde@particl.io"}
    usernames["victorwinberg"] |= {"victor.m.winberg@gmail.com"}
    usernames["NicoWeio"] |= {"nico.weio@gmail.com"}

    # read existing contributors, to avoid extra calls to the GitHub API
    if os.path.exists(filename):
        with open(filename, "r") as f:
            s = f.read()
        for line in s.split("\n"):
            if not line:
                continue
            username, *emails = line.split("\t")
            for email in emails:
                usernames[username].add(email)
        logger.info(f"Read {len(usernames)} contributors from {filename}")

    resolved_emails = set(
        email for email_set in usernames.values() for email in email_set
    )
    unresolved_emails = contributor_emails - resolved_emails
    for email in unresolved_emails:
        username_opt = _resolve_email(email)
        if username_opt:
            usernames[username_opt].add(email)

    with open(filename, "w") as f:
        for username, email_set in sorted(usernames.items()):
            emails_str = "\t".join(sorted(email_set))
            f.write(f"{username}\t{emails_str}")
            f.write("\n")

    logger.info(f"Wrote {len(usernames)} contributors to {filename}")

    email_to_username = {
        email: username for username, emails in usernames.items() for email in emails
    }

    return set(
        email_to_username[email]
        for email in contributor_emails
        if email in email_to_username
    )


def get_twitter_of_ghusers(ghusers: Collection[str]):
    logger.info("Getting twitter of GitHub usernames")
    twitter = {}
    for username in ghusers:
        try:
            resp = requests.get(f"https://api.github.com/users/{username}")
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"Failed to get twitter of {username}: {e}")
            continue

        twitter[username] = data["twitter_username"]
    return twitter


if __name__ == "__main__":
    build()
