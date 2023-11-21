#!/usr/bin/env python
import argparse
import datetime
import json
import re
import sys
import requests
from colorama import Fore, Style, init


def pattern_to_regex(pattern):
    """
    Converts a pattern string to a regular expression string.

    Args:
        pattern (str): The pattern string to convert.

    Returns:
        str: The regular expression string.
    """
    # Escape any special characters in the pattern
    escaped_pattern = re.escape(pattern)
    # Replace '*' with '.*' to match any sequence of characters
    regex_pattern = escaped_pattern.replace(r"\*", ".*")
    regex_pattern = regex_pattern.replace("\?", "?")
    # match from beginning till the end
    regex_pattern = f"^{regex_pattern}$"

    return regex_pattern


def generate_kernel_commits_from_github(
    username: str, repository: str, search_pattern: str = None, verbose=False
) -> list:
    """
    Generates a list of tuples containing branch names and their corresponding commit hashes for a given GitHub repository.

    Args:
        username (str): The username of the owner of the repository.
        repository (str): The name of the repository.
        search_pattern (str, optional): A regular expression pattern to filter branch names. Defaults to None.

    Returns:
        list: A list of tuples containing branch names and their corresponding commit hashes.
    """
    repo_url = f"https://api.github.com/repos/{username}/{repository}/branches?per_page=100"
    tags = []

    print(f"Fetching branches from {repo_url}")

    regex_pattern = None

    if search_pattern:
        regex_pattern = pattern_to_regex(search_pattern)

    while True:
        response = requests.get(repo_url)

        if response.status_code == 200:
            branches = response.json()

            for branch in branches:
                commit = branch["commit"]["sha"][:12]
                branch_name = branch["name"]
                if regex_pattern:
                    if re.match(regex_pattern, branch_name):
                        tags.append((branch_name, commit))
                else:
                    tags.append((branch_name, commit))
            if "next" in response.links:
                repo_url = response.links["next"]["url"]
            else:
                break
        else:
            print("Error:", response.status_code, response.text)
            break

    return tags


def get_kernel_tags_from_dockerhub(username, repository, search_pattern: str = None, verbose=False):
    """
    Retrieves Docker tags from Docker Hub for a given repository.

    Args:
        username (str): The Docker Hub username.
        repository (str): The name of the Docker repository.
        search_pattern (str, optional): A regular expression pattern to filter Docker tags. Defaults to None.
        verbose (bool, optional): Increase output verbosity if True. Defaults to False.

    Returns:
        list: A list of tuples containing Docker tags and their last push dates.
    """
    tags = []
    tags_url = (
        f"https://hub.docker.com/v2/repositories/{username}/{repository}/tags/?page_size=1000"
    )

    total_tags_fetched = 0
    regex_pattern = None

    # convert search patters to gerexp
    if search_pattern:
        regex_pattern = pattern_to_regex(search_pattern)

    while True:
        response = requests.get(tags_url)

        if response.status_code == 200:
            tags_json = response.json()
            count = tags_json["count"]
            # pretty print tags_json
            if verbose:
                print(json.dumps(tags_json, indent=4, sort_keys=True))

            raw_results = tags_json["results"]
            total_tags_fetched += len(raw_results)

            for tag in raw_results:
                if regex_pattern:
                    if re.match(regex_pattern, tag["name"]):
                        tags.append((tag["name"], tag["tag_last_pushed"]))
                else:
                    tags.append((tag["name"], tag["tag_last_pushed"]))

            # print progress overwrite the same line
            print(f"Fetching docker tags: {total_tags_fetched} / {count}", end="\r")

            if tags_json["next"]:
                tags_url = tags_json["next"]
                if verbose:
                    print(tags_url)
            else:
                # to keep progress on the screen
                print(f"Fetching docker tags: {total_tags_fetched} / {count}")
                break
        else:
            print("Error:", response.status_code, response.text)
            break
    return tags


def main():
    """
    Main function to compare kernel commits from Docker Hub and GitHub.
    """
    # Set Docker Hub username and repository name
    docker_username = "lfedge"
    gh_username = "lf-edge"
    repository = "eve-kernel"
    branch_search_pattern = "eve-kernel-*"

    # parse parameters. We support only -v - verbose mode
    parser = argparse.ArgumentParser(description="Process some integers.")
    parser.add_argument("-v", "--verbose", action="store_true", help="increase output verbosity")
    args = parser.parse_args()
    verbose = args.verbose

    # init colorama
    init(autoreset=True)

    tags = get_kernel_tags_from_dockerhub(
        docker_username, repository, search_pattern=branch_search_pattern, verbose=verbose
    )

    if len(tags) == 0:
        print("Error: no docker tags found matching pattern '{branch_search_pattern}'")
        sys.exit(1)

    # group tags by common capture group e.g. amd64-v6.1.38-generic
    tag_groups = {}
    for tag in tags:
        match = re.match(r"^eve-kernel-(.*)-[a-f0-9]+-gcc|clang$", tag[0])
        if match:
            capture_group = match.group(1)
            if capture_group not in tag_groups:
                tag_groups[capture_group] = []
            tag_groups[capture_group].append(tag)
        else:
            print(f"Warning: tag '{tag[0]}' doesn't match regex")

    # sort each group by date in descending order
    # so the first tag in each group is the most recent one
    for group in tag_groups:
        tag_groups[group].sort(key=lambda x: x[1], reverse=True)

    # print all tags with decoded dates
    if verbose:
        print("All tags:")
        for group in tag_groups:
            print(group)
            for tag in tag_groups[group]:
                # decode date from tag. Not really needed. To make sure we can handle date format
                date = datetime.datetime.strptime(tag[1], "%Y-%m-%dT%H:%M:%S.%fZ")
                print(f"\t{tag[0]} : {date.isoformat()}")

    # and collect (branch, commit) pairs by splitting tag name by '-'
    # and taking second last element as commit
    docker_commits = []
    for group in tag_groups:
        # take first tag from each group. The is the most recent one
        branch = tag_groups[group][0][0]
        _, _, arch, tag, flavor, commit, _ = branch.split("-")
        branch = f"eve-kernel-{arch}-{tag}-{flavor}"
        docker_commits.append((branch, commit))

    # print all kernel commits from docker hub
    if verbose:
        # sort commits from dockerhub by branch name
        docker_commits.sort(key=lambda x: x[0])
        print("Kernel commits from docker hub:")
        for branch, commit in docker_commits:
            print(f"{branch} : {commit}")

    # print all kernel commits from github
    gh_commits = generate_kernel_commits_from_github(
        gh_username, repository, search_pattern="eve-kernel-*", verbose=verbose
    )

    if verbose:
        print("Kernel commits from github:")
        gh_commits.sort(key=lambda x: x[0])
        for branch, commit in gh_commits:
            print(f"{branch} : {commit}")

    # compare kernel commits from docker hub and github
    # github may have more branches than corresponding docker hub tags
    # if commits for the same branch are equal, then we are good
    # otherwise we need to push new tags
    print(
        Fore.LIGHTYELLOW_EX
        + Style.BRIGHT
        + "Comparing tags from docker hub and latest commits from github:"
    )

    for branch, commit in gh_commits:
        if branch not in [x[0] for x in docker_commits]:
            print(Fore.RED + "[Error]" + Style.RESET_ALL + f": {branch} not found in docker hub")
        else:
            docker_commit = [x[1] for x in docker_commits if x[0] == branch][0]
            if commit != docker_commit:
                print(
                    Fore.RED
                    + "[Error]"
                    + Style.RESET_ALL
                    + f": {branch}: Update docker image {commit} -> {docker_commit}"
                )
            else:
                print(Fore.GREEN + "[  OK ]" + Style.RESET_ALL + f": {branch} : {commit}")


if __name__ == "__main__":
    main()
