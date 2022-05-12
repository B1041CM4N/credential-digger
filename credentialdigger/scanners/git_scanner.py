import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile

import hyperscan
from git import NULL_TREE, GitCommandError, InvalidGitRepositoryError
from git import Repo as GitRepo

from .base_scanner import BaseScanner, ResultHandler

logger = logging.getLogger(__name__)


class GitScanner(BaseScanner):
    def __init__(self, rules):
        """ Create the scanner for a git repository.

        The scanner compiles a list of rules, and uses hyperscan for regular
        expression matching.

        Parameters
        ----------
        rules: list
            A list of rules
        """
        super().__init__(rules)
        self.stream = rules

    @property
    def stream(self):
        return self._stream

    @stream.setter
    def stream(self, rules):
        """ Load the hyperscan database. """
        self._stream = hyperscan.Database(mode=hyperscan.HS_MODE_BLOCK)
        patterns = []
        for r in rules:
            rule_id, rule, _, _ = r.values()
            patterns.append((rule.encode('utf-8'),
                             rule_id,
                             hyperscan.HS_FLAG_CASELESS |
                             hyperscan.HS_FLAG_UTF8 |
                             hyperscan.HS_FLAG_UCP))

        expressions, ids, flags = zip(*patterns)
        self._stream.compile(expressions=expressions,
                             ids=ids,
                             elements=len(patterns),
                             flags=flags)

    def get_git_repo(self, repo_url, local_repo):
        """ Get a git repository.

        Parameters
        ----------
        repo_url: str
            The location of the git repository (an url if local is False, a
            local path otherwise)
        local_repo: bool
            If True, get the repository from a local directory instead of the
            web

        Returns
        -------
        str
            The temporary path to which the repository has been copied
        GitRepo
            The repository object

        Raises
        ------
        FileNotFoundError
            If repo_url is not an existing directory
        git.InvalidGitRepositoryError
            If the directory in repo_url is not a git repository
        git.GitCommandError
            If the url in repo_url is not a git repository, or access to the
            repository is denied
        """
        if local_repo:
            logger.warning('The parameter --local is now redundant,'
                           'and will be deprecated. Local repos can be scanned'
                           'with the scan option as if they were remote ones.'
                           'They are automatically managed by the tool.')

        project_path = tempfile.mkdtemp()

        try:
            repo = GitRepo.clone_from(repo_url, project_path)
        except GitCommandError as e:
            logger.warning('Repo can not be cloned')
            shutil.rmtree(project_path)
            raise e
        except FileNotFoundError as e:
            shutil.rmtree(project_path)
            raise e
        except InvalidGitRepositoryError as e:
            shutil.rmtree(project_path)
            raise e

        return project_path, repo

    def get_commit_id_from_branch(self, repo, branch_name):
        """ Get the commit id of the last commit pushed to a branch.

        Parameters
        ----------
        repo: `GitPython.Repo`
            The repository object
        branch_name: str
            The name of a branch of this repo

        Returns
        -------
        str
            The commit id
        """
        try:
            # If branch_or_commit is a branch name, we have to find the
            # corresponding commit id
            # There may be more refs to a branch. In this case, let's consider
            # the first
            commit_to = repo.git.show_ref(
                branch_name, hash=True).split('\n')[0].strip()
            logger.debug(f'Branch {branch_name} refers to commit id '
                         f'{commit_to}')
        except GitCommandError:
            # branch_name was already a commit id
            commit_to = branch_name
        return commit_to

    def get_commit_timestamp(self, repo_url, branch_or_commit,
                             git_username=None, git_token=None):
        """ Get the timestamp of the commit id of a repo.

        In case `branch_or_commit` is a branch name, it will be converted into
        the corresponding commit id (i.e., the most recent commit done on this
        branch) and its timestamp is returned.

        Parameters
        ----------
        repo_url: str
            The url of a repository
        branch_or_commit: str
            The branch name or commit id of the repo
        git_username: str, optional
            the username of the user to authenticate to the git server. While
            it is not needed for `github.com` and github enterprise, it is
            needed for some private git instances and bitbucket
        git_token: str, optional
            The personal user access token to access to this repo (needed for
            private repos)

        Returns
        -------
        int
            The timestamp of the chosen commit
        """
        if git_token:
            logger.debug('Authenticate user with token')
            username = git_username or 'oauth2'
            repo_url = repo_url.replace('https://',
                                        f'https://{username}:{git_token}@')

        # TODO: once local repos are supported in scan_snapshot, we will have
        # to pass local_repo as argument
        project_path, repo = self.get_git_repo(repo_url, local_repo=False)

        # Get the commit_id in case the `branch_or_commit` parameter is a
        # branch name
        commit_id = self.get_commit_id_from_branch(repo, branch_or_commit)

        # Get the commit timestamp
        commit_date = int(repo.git.show(commit_id, format='%ct', quiet=True
                                        ).strip())
        # Delete repo folder
        shutil.rmtree(project_path)

        return commit_date

    def scan(self, repo_url, since_timestamp=0, max_depth=1000000,
             git_username=None, git_token=None, local_repo=False, debug=False):
        """ Scan a repository.

        Parameters
        ----------
        repo_url: str
            The location of a git repository (an url if local_repo is False, a
            local path otherwise)
        since_timestamp: int, optional
            The oldest timestamp to scan
        max_depth: int, optional
            The maximum number of commits to scan
        git_username: str, optional
            the username of the user to authenticate to the git server. While
            it is not needed for `github.com` and github enterprise, it is
            needed for some private git instances and bitbucket
        git_token: str, optional
            Git personal access token to authenticate to the git server
        local_repo: bool, optional
            If True, get the repository from a local directory instead of the
            web
        debug: bool, optional
            If True, visualize debug information during the scan

        Returns
        -------
        list
            A list of discoveries (dictionaries). If there are no discoveries
            return an empty list
        """
        if debug:
            logger.setLevel(level=logging.DEBUG)

        if git_token:
            logger.debug('Authenticate user with token')
            username = git_username or 'oauth2'
            repo_url = repo_url.replace('https://',
                                        f'https://{username}:{git_token}@')

        project_path, repo = self.get_git_repo(repo_url, local_repo)
        discoveries = self._scan(repo, since_timestamp, max_depth)

        # Delete repo folder
        shutil.rmtree(project_path)

        # Generate a list of discoveries and return it.
        # N.B.: This may become inefficient when the discoveries are many.
        return discoveries

    def _scan(self, repo, since_timestamp, max_depth):
        """ Perform the actual scan of the repository.

        Parameters
        ----------
        repo: `git.GitRepo`
            The repository object
        since_timestamp: int
            The oldest timestamp to scan
        max_depth: int
            The maximum number of commits to scan

        Returns
        -------
        list
            A list of discoveries (dictionaries). If there are no discoveries
            return an empty list
        """
        already_searched = set()
        discoveries = []

        branches = repo.remotes.origin.fetch()

        logger.debug('Scanning commits...')
        for remote_branch in branches:
            branch_name = remote_branch.name
            logger.debug(f'Branch {branch_name} in progress...')
            prev_commit = None
            # Note that the iteration of the commits is backwards, so the
            # prev_commit is newer than curr_commit
            for curr_commit in repo.iter_commits(branch_name,
                                                 max_count=max_depth):
                # if not prev_commit, then curr_commit is the newest commit
                # (and we have nothing to diff with).
                # But we will diff the first commit with NULL_TREE here to
                # check the oldest code. In this way, no commit will be missed.
                if not prev_commit:
                    # The current commit is the latest one
                    prev_commit = curr_commit
                    continue

                if prev_commit.committed_date <= since_timestamp:
                    # We have reached the (chosen) oldest timestamp, so
                    # continue with another branch
                    break

                # This is useful for git merge: in case of a merge, we have the
                # same commits (prev and current) in two different branches.
                # This trick avoids scanning twice the same commits
                diff_hash = hashlib.md5((str(prev_commit) + str(curr_commit))
                                        .encode('utf-8')).digest()
                if diff_hash in already_searched:
                    prev_commit = curr_commit
                    continue
                else:
                    # Avoid searching the same diffs
                    already_searched.add(diff_hash)

                # Get the diff between two commits
                # Ignore possible submodules (they are independent from
                # this repo)
                diff = curr_commit.diff(prev_commit,
                                        create_patch=True,
                                        ignore_submodules='all',
                                        ignore_all_space=True,
                                        unified=0,
                                        diff_filter='AM')

                # Diff between the current commit and the previous one
                discoveries.extend(self._diff_worker(diff, prev_commit))

                prev_commit = curr_commit

            # Handling the first commit (either from since_timestamp or the
            # oldest).
            # If `since_timestamp` is set, then there is no need to scan it
            # (because we have already scanned this diff at the previous step).
            # If `since_timestamp` is 0, we have reached the first commit of
            # the repo, and the diff here must be calculated with an empty tree
            if since_timestamp == 0:
                diff = curr_commit.diff(NULL_TREE,
                                        create_patch=True,
                                        ignore_submodules='all',
                                        ignore_all_space=True)

                discoveries = discoveries + \
                    self._diff_worker(diff, prev_commit)
        return discoveries

    def _diff_worker(self, diff, commit):
        """ Compute the diff between two commits.

        Parameters
        ----------
        diff: string
            The diff introduced by a commit
        commit: string
            The commit the diff is calculated on

        Returns
        -------
        list of dictionaries
            A list of discoveries
        """
        detections = []
        for blob in diff:
            # new file: a_path is None, deleted file: b_path is None
            old_path = blob.b_path if blob.b_path else blob.a_path

            printable_diff = blob.diff.decode('utf-8', errors='replace')

            if printable_diff.startswith('Binary files'):
                # Do not scan binary files
                continue

            detections = detections + self._regex_check(printable_diff,
                                                        old_path,
                                                        commit.hexsha)
        return detections

    def _regex_check(self, printable_diff, filename, commit_hash):
        """ Scan the diff with regexes.

        Here the scan is performed.
        Lines that are either longer than 500 characters or that are not newly
        added are not scanned. Indeed, if a line is scanned as soon as it is
        added to the file, there is no need to need to re-scan the same line
        when (and if) it gets deleted.
        In addition to this, lines longer than 500 characters usually represent
        the content of an entire file on the same line. This (almost always)
        produces a false positive discovery (thus we assume we can avoid
        scanning them).

        Parameters
        ----------
        printable_diff: string
            The diff of two commits
        filename: string
            The name of the file that contains the diff
        commit_hash: string
            The hash of the commit (from git)

        Returns
        -------
        list
            A list of dictionaries (each dictionary is a discovery)
        """
        detections = []
        r_hunkheader = re.compile(r'@@\s*\-\d+(\,\d+)?\s\+(\d+)((\,\d+)?).*@@')
        r_hunkaddition = re.compile(r'^\+\s*(\S(.*\S)?)\s*$')
        rows = printable_diff.splitlines()
        line_number = 1
        for row in rows:
            if row.startswith('-') or len(row) > 500:
                # Take into consideration only added lines that are shorter
                # than 500 characters
                continue
            if row.startswith('@@'):
                # If the row is a git diff hunk header, get the first addition
                # line number in the header and go to the next line
                r_groups = re.search(r_hunkheader, row)
                if r_groups is not None:
                    line_number = int(r_groups.group(2))
                    continue
            elif row.startswith('+'):
                # Remove '+' character from diff hunk and trim row
                r_groups = re.search(r_hunkaddition, row)
                if r_groups is not None:
                    row = r_groups.group(1)

            rh = ResultHandler()
            self.stream.scan(
                row if sys.version_info < (3, 8) else row.encode('utf-8'),
                match_event_handler=rh.handle_results,
                context=[row, filename, commit_hash, line_number])
            if rh.result:
                detections.append(rh.result)
            line_number += 1
        return detections
