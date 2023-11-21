#!/usr/bin/env python3
import argparse
import json
import os
import pty
import re
import tempfile
import git
from git import Commit, Head, PushInfo
from git import RemoteReference
from git import Remote
from git.remote import FetchInfo
from git.exc import GitCommandError
from github import Github
from github import Auth
from github.Repository import Repository
from github import PullRequest
import logging
import github
import requests

# Define the path to the configuration file
config_file_path = os.path.expanduser("~/.config/eve-ci/gh.json")


# Function to read the GitHub token from the config file
def read_github_token_from_config():
    if os.path.exists(config_file_path):
        with open(config_file_path, "r") as config_file:
            config_data = json.load(config_file)
            return config_data.get("gh-token")
    return None


# Function to write the GitHub token to the config file
def write_github_token_to_config(token):
    os.makedirs(os.path.dirname(config_file_path), exist_ok=True)
    with open(config_file_path, "w") as config_file:
        json.dump({"gh-token": token}, config_file)


# Function to interactively prompt the user for a GitHub token
def get_github_token_from_user():
    return input("Enter your GitHub personal access token: ")


def get_github_token(token):
    if token:
        github_token = token
        write_github_token_to_config(github_token)
    else:
        github_token = read_github_token_from_config()

    if not github_token:
        print("GitHub personal access token is required.")
        github_token = get_github_token_from_user()
        write_github_token_to_config(github_token)
    return github_token


def parse_cmd_args():
    parser = argparse.ArgumentParser(description="Automate PR merge process.")
    parser.add_argument("-t", "--token", help="GitHub personal access token", required=False)
    parser.add_argument(
        "-p",
        "--pr",
        help="Source pull request number to create copies from",
        type=int,
        required=True,
    )
    parser.add_argument(
        "-b",
        "--branches",
        help="Comma-separated list of target branches",
        required=False,
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        help="Dry run. Do not create PRs and branches",
        action="store_true",
        required=False,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        help="Verbose output",
        action="store_true",
        required=False,
    )
    args = parser.parse_args()
    return args


def get_github_repo(g: Github, user, repo_name):
    gh_repo = g.get_repo(f"{user}/{repo_name}")
    return gh_repo


def get_github_parent_repo(g: Github, gh_repo: Repository) -> Repository:
    gh_parent_repo = gh_repo.parent
    if not gh_parent_repo:
        raise Exception(f"Failed to get parent repository for {gh_repo.name}")
    return gh_parent_repo


def open_git_repo(path: str = "./"):
    git_repo = git.Repo(path)
    if not git_repo:
        raise Exception(f"Failed to open git repository at {path}")

    # check that this is a github repo
    git_remote_url = git_repo.remotes.origin.url
    # parse the git remote url which can be an ssh or https url
    # ssh: git@github...
    # https: https://github...
    # we want to get the github repo name and owner
    # ssh: github.com/<owner>/<repo>.git
    # https: github.com/<owner>/<repo>.git
    if "github.com" not in git_remote_url:
        raise Exception(f"Remote URL {git_remote_url} is not a GitHub repository")
    owner, repo_name = git_remote_url.split("github.com/")[1].replace(".git", "").split("/")
    return owner, repo_name, git_repo


def sync_fork_branches(
    g: Github,
    fork: Repository,
    parent: Repository,
    branches_to_sync: list,
    dry_run: bool = False,
):
    for branch_name in branches_to_sync:
        try:
            # Check if the branch already exists in the fork
            existing_branch = fork.get_branch(branch_name)

            if existing_branch:
                # If the branch exists, update it with the latest commit from the upstream repository
                upstream_branch = parent.get_branch(branch_name)
                ref = fork.get_git_ref(f"heads/{branch_name}")
                new_sha = upstream_branch.commit.sha
                current_sha = ref.object.sha

                if current_sha == new_sha:
                    print(f"Branch {branch_name} is up-to-date")
                    continue
                if not dry_run:
                    ref.edit(new_sha)
                    print(f"Updated branch: to  {branch_name}: {current_sha} -> {new_sha}")
                else:
                    print(
                        f"[DRY RUN]: Would update branch: {branch_name} {current_sha} -> {new_sha}"
                    )
            else:
                # FIXME: this code is never executed
                print(f"Branch {branch_name} does not exist in the fork.")
        except Exception as e:
            # If the branch doesn't exist, create it
            upstream_branch = parent.get_branch(branch_name)
            fork.create_git_ref(ref=f"refs/heads/{branch_name}", sha=upstream_branch.commit.sha)
            print(f"Synced branch: {branch_name}")


def pattern_to_regex(pattern):
    # Escape any special characters in the pattern
    escaped_pattern = re.escape(pattern)
    # Replace '*' with '.*' to match any sequence of characters
    regex_pattern = escaped_pattern.replace(r"\*", ".*")

    return regex_pattern


# branch can be a pattern like eve-kernel-* or eve-kernel-*-v6.1.38-*.
def expand_branch_patterns(upstream, target_branches):
    upstream_branches = {branch.name for branch in upstream.get_branches()}

    print(f"Found branches in {upstream.owner.login}/{upstream.name}:")
    for branch in upstream_branches:
        print(f"\t{branch}")

    expanded_branches = set()
    for branch_pattern in target_branches:
        if "*" in branch_pattern:
            # Get all branches that match this pattern (may be zero matches)
            branches = {
                branch
                for branch in upstream_branches
                if re.match(pattern_to_regex(branch_pattern), branch)
            }
            expanded_branches.update(branches)
        elif branch_pattern in upstream_branches:
            expanded_branches.add(branch_pattern)
        else:
            raise Exception(
                f"Branch {branch_pattern} does not exist in upstream repository {upstream.name}"
            )

    return expanded_branches


def create_local_branch(git_repo: git.Repo, base_branch_name: str, pr_number: int) -> Head:
    local_branch_name = f"pr/{pr_number}/{base_branch_name}"
    print(
        f"Creating local branch {local_branch_name} for PR# {pr_number} from origin/{base_branch_name}"
    )

    if local_branch_name in git_repo.heads:
        print(f"Branch {local_branch_name} already exists. Skipping...")
        return git_repo.heads[local_branch_name]

    create_from = RemoteReference(git_repo, f"refs/remotes/origin/{base_branch_name}")
    if create_from.is_valid():
        print(f"Remote ref {create_from} is valid")

    new_branch = git_repo.create_head(local_branch_name, create_from)
    new_branch.set_commit(create_from.commit)
    # if remote branch for newly created branch exists, set tracking branch
    remote_ref = RemoteReference(git_repo, f"refs/remotes/origin/{local_branch_name}")
    if remote_ref.is_valid():
        new_branch.set_tracking_branch(remote_ref)

    # TODO: probably it is better to delete local branch if it already exists
    # FIXME: it doesn't work correctly. git complains
    # Your branch is based on 'origin/master', but the upstream is gone.
    #   (use "git branch --unset-upstream" to fixup)
    # new_branch = git_repo.create_head(
    #     local_branch_name, git_repo.remotes.origin.refs[f"{new_branch_name}"]
    # )
    # Head.set_reference(new_branch, f"refs/heads/{local_branch_name}")
    # FIXME: it seems it is redundant
    # rem_ref = RemoteReference(git_repo, f"refs/remotes/origin/{local_branch_name}")
    # branch.set_tracking_branch(rem_ref)
    return new_branch


def get_pr_diff(pr):
    diff_url = pr.url
    # FIXME: we have to query the API to get the diff because PyGithub does not support it yet
    response = requests.get(diff_url, headers={"Accept": "application/vnd.github.v3.patch"})
    if response.status_code == 200:
        return response.text
    else:
        raise Exception(f"Failed to fetch diff for PR {pr}")


def get_pr_diff_file(pr):
    path = os.path.join(tempfile.mkdtemp(), f"merge-pr-{pr.number}.patch")
    diff = get_pr_diff(pr)
    with open(path, "w") as diff_file:
        diff_file.write(diff)
    return path


def is_patch_already_applied(git_repo, diff_file_path) -> bool:
    try:
        output = git_repo.git.apply("--check", "--reverse", diff_file_path)
    except GitCommandError as e:
        if e.status == 1 and "patch does not apply" in e.stderr.strip():
            return False
    return True


def create_pull_request(
    repo: Repository,
    fork_owner,
    original_pr: PullRequest,
    source_branch: str,
    target_branch: str,
):
    # for cross-repository PRs we need to specify head as <fork_owner>:<source_branch>
    # see https://docs.github.com/en/rest/pulls/pulls?apiVersion=2022-11-28#create-a-pull-request
    head = f"{fork_owner}:{source_branch}"
    # short branch name without prefix eve-kernel-
    short_branch = source_branch.split("-", 1)[1]
    title = f"[Merge PR#{original_pr.number} -> {short_branch}] {original_pr.title}"

    # Create a new pull request
    pr = repo.create_pull(
        title=title,
        base=target_branch,
        head=head,
        body=f"Automated PR merge. See [PR#{original_pr.number}]({original_pr.html_url})",
    )
    return pr


def pr_exists(repo: Repository, fork_owner, source_branch: str, target_branch: str):
    # for cross-repository PRs we need to specify head as <fork_owner>:<source_branch>
    # see https://docs.github.com/en/rest/reference/pulls#list-pull-requests
    head = f"{fork_owner}:{source_branch}"

    print(f"Checking if PR exists for {head} -> {target_branch}")
    prs = repo.get_pulls(base=target_branch, head=head, state="open")
    if prs.totalCount > 0:
        return True

    prs = repo.get_pulls(base=target_branch, head=head, state="merged")
    if prs.totalCount > 0:
        return True

    return False


def get_commits_to_cherry_pick(repo: git.Repo, pr: PullRequest) -> list[Commit]:
    """
    Returns a list of commits to cherry-pick from a given PullRequest object.

    Args:
        repo: The repository object.
        pr (PullRequest): The PullRequest object.

    Returns:
        A list of commits to cherry-pick from the given PullRequest object.
    """
    # get a list of commits to cherry-pick from PR
    # commits are taken from mergeCommit field of MERGED pr
    real_commits = []
    if pr.merged:
        print(f"PR# {pr.number} is merged")
        commits_to_cherry_pick = [commit.sha for commit in pr.get_commits()]
        print(f"Merge commit SHA: {pr.merge_commit_sha}")
        real_commits = repo.iter_commits(pr.merge_commit_sha, max_count=len(commits_to_cherry_pick))
        # make a list out of generator and reverse it
        real_commits = list(reversed(list(real_commits)))
    return real_commits


def print_fetch_info(info):
    for ref in info:
        if ref.flags & FetchInfo.HEAD_UPTODATE:
            print(f"\tBranch {ref.ref} is up-to-date")
        else:
            print(f"\tUpdated {ref.ref} from {ref.old_commit} to {ref.commit}")


def pr_cherry_pick(local_git_repo, pr_to_clone, merged_commits, branch) -> bool:
    # Auto-merging .github/workflows/publish.yml
    # CONFLICT (content): Merge conflict in .github/workflows/publish.yml
    # Auto-merging Makefile.eve
    # error: could not apply a00c3fbfbad9... modified the conditonal trigger, removed an extra docker login command
    # hint: After resolving the conflicts, mark them with
    # hint: "git add/rm <pathspec>", then run
    # hint: "git cherry-pick --continue".
    # hint: You can instead skip this commit with "git cherry-pick --skip".
    # hint: To abort and get back to the state before "git cherry-pick",
    # hint: run "git cherry-pick --abort".

    print(f"Cherry-picking commits from PR# {pr_to_clone.number} to branch {branch}")
    print(merged_commits)
    for commit in merged_commits:
        commit_hash = commit.hexsha[:12]
        print(f"Cherry-picking commit {commit_hash}")
        try:
            local_git_repo.git.cherry_pick(commit, "-x", "-s")
        except GitCommandError as e:
            print(f"Failed automatically to cherry-pick commit {commit} to branch {branch}")
            print(f"STDOUT:\n'{e.stdout.strip()}'")
            print(f"STDERR:\n'{e.stderr.strip()}'")
            # if re.match(
            #     f"error: could not apply {commit_hash}", e.stderr.strip(), flags=re.M | re.I
            # ):
            # run interactive shell to resolve conflicts
            # run bash process
            pty.spawn(["/bin/bash"])

            # ask user whether cherry-pick was successful
            merge_successful = input(
                f"Was cherry-pick successful for commit {commit_hash}? [y/N]: "
            ).lower()

            merge_successful = merge_successful in ["y", "yes"]
            if not merge_successful:
                return False

        except Exception as e:
            print("Raised exception")
            raise e

        # print(f"git cherry-pick output: {output}")

    return True


def validate_local_repo(fork_user, owner, local_git_repo):
    if owner != fork_user:
        raise Exception(
            f"Git repository owner {owner} does not match GitHub user {fork_user}. Wrong origin?"
        )

    if local_git_repo.is_dirty():
        raise Exception(
            f"Git repository at {local_git_repo.working_dir} is dirty. Please commit or stash your changes."
        )


def print_matching_branches(target_branches, upstream):
    print(f"Found branches in {upstream.owner.login}/{upstream.name} matching patterns:")
    for branch in target_branches:
        print(f"\t{branch}")


def print_commit_list(merged_commits: list[Commit]):
    for commit in merged_commits:
        commit_title = commit.message.split("\n")[0]
        print(f"\tCommit: {commit.hexsha[:12]}: {commit_title}")


def pr_get_label_list(pr):
    labels = []
    for label in pr.labels:
        labels.append(label.name)
    return labels


def is_pr_labeled_merged(pr):
    labels = pr_get_label_list(pr)
    if "pr-merged" in labels:
        return True
    return False


def print_pr_info(pr):
    print(f"Found PR# {pr.number} with target branch {pr.base.ref}")
    print(f"PR URL: {pr.html_url}")
    print(f"PR state: {pr.state}")
    if pr.merged:
        print(f"PR merge commit SHA: {pr.merge_commit_sha}")
    print(f"Title: {pr.title}")
    print(f"PR labels: {pr_get_label_list(pr)}")


# main function
def main():
    current_fork_branch = None
    diff_file_path = None
    target_branches = []

    try:
        # github.enable_console_debug_logging()
        args = parse_cmd_args()
        if args.verbose:
            logging.basicConfig(level=logging.DEBUG)
        else:
            logging.basicConfig(level=logging.INFO)

        # Initialize the GitHub API client
        github_user_token = get_github_token(args.token)
        g = Github(auth=Auth.Token(github_user_token))
        fork_user = g.get_user().login
        print(f"Current logged in GH user: {fork_user}")

        owner, fork_name, local_git_repo = open_git_repo()
        validate_local_repo(fork_user, owner, local_git_repo)

        print(f"Opening fork {fork_name} for user {fork_user}")
        fork = get_github_repo(g, owner, fork_name)
        # save current branch
        current_fork_branch = local_git_repo.active_branch

        try:
            print(f"Getting parent repository for {fork_user}/{fork_name}")
            upstream = get_github_parent_repo(g, fork)
        except Exception as e:
            print(
                f"Failed to get parent repository for {fork_user}/{fork_name}. Is this really a fork?"
            )
            raise e

        print(
            f"Found upstream repository {upstream.owner.login}/{upstream.name} for {fork_user}/{fork_name}"
        )

        # get an original target branch for PR. It will be skipped later
        pr_to_clone = upstream.get_pull(args.pr)
        print_pr_info(pr_to_clone)

        if is_pr_labeled_merged(pr_to_clone):
            raise Exception(f"PR {pr_to_clone.number} is already merged to all target branches")

        # branches from command line have higher priority
        if args.branches is not None:
            target_branches = args.branches.split(",")
        else:
            # get branches from PR labels
            labels = pr_get_label_list(pr_to_clone)
            target_branches = labels_to_branches(labels)
        # branch can be a pattern like eve-kernel-* or eve-kernel-*-v6.1.38-*.
        # We need to get all branches that match either patterns or exactly
        target_branches = expand_branch_patterns(upstream, target_branches)

        # no branches - no candies
        if len(target_branches) == 0:
            raise Exception(
                f"No branches found in {upstream.owner.login}/{upstream.name} matching patterns"
            )

        print_matching_branches(target_branches, upstream)

        if pr_to_clone.base.ref in target_branches:
            print(f"Skipping target branch {pr_to_clone.base.ref} for PR# {pr_to_clone.number}")
            target_branches.remove(pr_to_clone.base.ref)

        sync_fork_branches(g, fork, upstream, target_branches, args.dry_run)

        # fetch branches from origin
        print(f"Fetching branches from origin...")
        info = local_git_repo.remotes.origin.fetch("--dry-run" if args.dry_run else None)
        print_fetch_info(info)
        # FIXME: pr.merge_commit_sha is available in local 'upstream' only
        # so get_commits_to_cherry_pick fails without fetching 'upstream'
        info = local_git_repo.remotes.upstream.fetch("--dry-run" if args.dry_run else None)
        print_fetch_info(info)

        # get a list of commits to cherry-pick from MERGED PR
        # these commits have all conflicts resolver and should apply cleanly (but not always)
        print(f"Getting commits to cherry-pick from PR# {pr_to_clone.number}")
        merged_commits = get_commits_to_cherry_pick(local_git_repo, pr_to_clone)
        print_commit_list(merged_commits)

        # fetch diff for PR into temporary file
        diff_file_path = get_pr_diff_file(pr_to_clone)
        print(f"Diff for PR# {pr_to_clone.number} is saved to {diff_file_path}")

        if pr_to_clone.merged:
            print("STRATEGY: cherry-pick merged commits")
        else:
            print("STRATEGY: git am -3")

        # 1. create a local branch for each target branch in format pr/<pr_number>/<target_branch>
        # 2. apply patch from PR to each local branch is not already applied
        # 3. push local branch to fork
        # 4. create a PR for each local branch

        if not args.dry_run:
            for branch in target_branches:
                local_branch = create_local_branch(local_git_repo, branch, pr_to_clone.number)

                # check if PR already exists for a combination of source and target branches
                if pr_exists(upstream, fork_user, local_branch.name, branch):
                    print(
                        f"PR already exists for branch {branch} and source branch {local_branch.name}"
                    )
                    continue

                local_git_repo.git.checkout(local_branch)

                if pr_to_clone.merged:
                    if pr_cherry_pick(local_git_repo, pr_to_clone, merged_commits, branch):
                        # else:
                        #     if not is_patch_already_applied(local_git_repo, diff_file_path):
                        #         print(f"Applying diff from PR# {pr_to_clone.number} to branch {branch}")
                        #         try:
                        #             output = local_git_repo.git.am("-3", diff_file_path)
                        #         except GitCommandError as e:
                        #             print(f"Failed to apply diff to branch {branch}")
                        #             print(f"STDERR: {e.stderr}")
                        #             print(f"STDOUT: {e.stdout}")
                        #             print(f"Return code: {e.status}")
                        #             raise e

                        #         print(f"git am -3 output: {output}")
                        #     else:
                        #         print(
                        #             f"Patch from PR# {pr_to_clone.number} is already applied to branch {local_branch}"
                        #         )

                        print(f"Pushing local branch {local_branch} to {fork_user}/{fork_name}")
                        push_info = local_git_repo.remotes.origin.push(local_branch, force=True)

                        for i in push_info:
                            if i.flags & PushInfo.UP_TO_DATE:
                                print(f"\tBranch {i.remote_ref_string} is up-to-date")
                            elif i.flags & PushInfo.FAST_FORWARD:
                                print(
                                    f"\tUpdated {i.remote_ref_string} from {i.old_commit} to {i.local_ref.commit}"
                                )
                            elif i.flags & PushInfo.NEW_HEAD:
                                print(f"\tCreated {i.remote_ref_string} at {i.local_ref.commit}")
                            elif i.flags & PushInfo.FORCED_UPDATE:
                                print(
                                    f"\tUpdated [FORCED] {i.remote_ref_string} from {i.old_commit} to {i.local_ref.commit}"
                                )

                        # check if PR already exists
                        # TODO: maybe update existing PR?
                        if not pr_exists(upstream, fork_user, local_branch.name, branch):
                            print(f"Creating PR for branch {local_branch}")
                            # ask user whether to create PR
                            create_pr = input(
                                f"Create PR for branch {local_branch}? [y/N]: "
                            ).lower()
                            create_pr = create_pr in ["y", "yes"]
                            if create_pr:
                                new_pr = create_pull_request(
                                    upstream, fork_user, pr_to_clone, local_branch.name, branch
                                )
                                print(f"PR created: {new_pr.html_url}")
            pr_mark_merged(pr_to_clone, upstream, fork_user)
        else:
            print(f"[DRY RUN]: Would checkout {local_branch}")
            print(
                f"[DRY RUN]: Would apply diff from PR# {pr_to_clone.number} to branch {local_branch}"
            )
            print(f"[DRY RUN]: Would push local branch {local_branch} to {fork_user}/{fork_name}")
            print(f"[DRY RUN]: Would create PR for branch {local_branch}")

    except:
        raise
    finally:
        # restore original branch
        if not args.dry_run:
            if current_fork_branch is not None:
                local_git_repo.git.checkout(current_fork_branch)
            # remove temporary file
            if diff_file_path not in [None, ""] and os.path.exists(diff_file_path):
                os.remove(diff_file_path)
        else:
            print(f"[DRY RUN]: Would checkout {current_fork_branch}")
            print(f"[DRY RUN]: Would remove {diff_file_path}")


def labels_to_branches(labels):
    target_branches = []
    for label in labels:
        if label.startswith("pr:"):
            branch = label.split("pr:")[1].strip()
            target_branches.append(branch)
    return target_branches


def pr_mark_merged(pr_to_clone, repo, fork_user):
    new_labels = pr_get_label_list(pr_to_clone)
    branches = labels_to_branches(new_labels)
    # remove original PR branch
    # the check is redundant
    if pr_to_clone.base.ref in branches:
        branches.remove(pr_to_clone.base.ref)

    merged = True

    for branch in branches:
        local_branch = f"pr/{pr_to_clone.number}/{branch}"
        if not pr_exists(repo, fork_user, local_branch, branch):
            merged = False
            break

    if merged:
        new_labels.append("pr-merged")
        pr_to_clone.set_labels(new_labels)


if __name__ == "__main__":
    main()
    # try:
    #     main()
    # except Exception as e:
    #     print(f"ERROR: {e}")
    #     exit(1)
