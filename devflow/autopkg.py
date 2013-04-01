# Copyright 2012, 2013 GRNET S.A. All rights reserved.
#
# Redistribution and use in source and binary forms, with or
# without modification, are permitted provided that the following
# conditions are met:
#
#   1. Redistributions of source code must retain the above
#      copyright notice, this list of conditions and the following
#      disclaimer.
#
#   2. Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials
#      provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY GRNET S.A. ``AS IS'' AND ANY EXPRESS
# OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL GRNET S.A OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF
# USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
# AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and
# documentation are those of the authors and should not be
# interpreted as representing official policies, either expressed
# or implied, of GRNET S.A.

"""Helper script for automatic build of debian packages."""

import os
import sys
from optparse import OptionParser
from sh import mktemp, cd, rm, git_dch  # pylint: disable=E0611
from configobj import ConfigObj

from devflow import versioning
from devflow import utils
from devflow import BRANCH_TYPES

if sys.stdout.isatty():
    try:
        import colors
        use_colors = True
    except AttributeError:
        use_colors = False


if use_colors:
    red = colors.red
    green = colors.green
else:
    red = lambda x: x
    green = lambda x: x

print_red = lambda x: sys.stdout.write(red(x) + "\n")
print_green = lambda x: sys.stdout.write(green(x) + "\n")

AVAILABLE_MODES = ["release", "snapshot"]

DESCRIPTION = """Tool for automatical build of debian packages.

%(prog)s is a helper script for automatic build of debian packages from
repositories that follow the `git flow` development model
<http://nvie.com/posts/a-successful-git-branching-model/>.

This script must run from inside a clean git repository and will perform the
following steps:
    * Clone your repository to a temporary directory
    * Merge the current branch with the corresponding debian branch
    * Compute the version of the new package and update the python
      version files
    * Create a new entry in debian/changelog, using `git-dch`
    * Create the debian packages, using `git-buildpackage`
    * Tag the appropriate branches if in `release` mode

%(prog)s will work with the packages that are declared in `autopkg.conf`
file, which must exist in the toplevel directory of the git repository.

"""


def print_help(prog):
    print DESCRIPTION % {"prog": prog}


def main():
    from devflow.version import __version__  # pylint: disable=E0611,F0401
    parser = OptionParser(usage="usage: %prog [options] mode",
                          version="devflow %s" % __version__,
                          add_help_option=False)
    parser.add_option("-h", "--help",
                      action="store_true",
                      default=False,
                      help="show this help message")
    parser.add_option("-k", "--keep-repo",
                      action="store_true",
                      dest="keep_repo",
                      default=False,
                      help="Do not delete the cloned repository")
    parser.add_option("-b", "--build-dir",
                      dest="build_dir",
                      default=None,
                      help="Directory to store created pacakges")
    parser.add_option("-r", "--repo-dir",
                      dest="repo_dir",
                      default=None,
                      help="Directory to clone repository")
    parser.add_option("-d", "--dirty",
                      dest="force_dirty",
                      default=False,
                      action="store_true",
                      help="Do not check if working directory is dirty")
    parser.add_option("-c", "--config-file",
                      dest="config_file",
                      help="Override default configuration file")
    parser.add_option("--no-sign",
                      dest="sign",
                      action="store_false",
                      default=True,
                      help="Do not sign the packages")
    parser.add_option("--key-id",
                      dest="keyid",
                      help="Use this keyid for gpg signing")
    parser.add_option("--dist",
                      dest="dist",
                      default="unstable",
                      help="If running in snapshot mode, automatically set"
                           " the changelog distribution to this value"
                           " (default=unstable).")

    (options, args) = parser.parse_args()

    if options.help:
        print_help(parser.get_prog_name())
        parser.print_help()
        return

    # Get build mode
    try:
        mode = args[0]
    except IndexError:
        mode = utils.get_build_mode()
    if mode not in AVAILABLE_MODES:
        raise ValueError(red("Invalid argument! Mode must be one: %s"
                         % ", ".join(AVAILABLE_MODES)))

    os.environ["DEVFLOW_BUILD_MODE"] = mode

    # Load the repository
    original_repo = utils.get_repository()

    # Check that repository is clean
    toplevel = original_repo.working_dir
    if original_repo.is_dirty() and not options.force_dirty:
        raise RuntimeError(red("Repository %s is dirty." % toplevel))

    # Get packages from configuration file
    config_file = options.config_file or os.path.join(toplevel, "devflow.conf")
    config = ConfigObj(config_file)
    packages = config['packages'].keys()
    print_green("Will build the following packages:\n" + "\n".join(packages))

    # Get current branch name and type and check if it is a valid one
    branch = original_repo.head.reference.name
    branch_type_str = versioning.get_branch_type(branch)

    if branch_type_str not in BRANCH_TYPES.keys():
        allowed_branches = ", ".join(BRANCH_TYPES.keys())
        raise ValueError("Malformed branch name '%s', cannot classify as"
                         " one of %s" % (branch, allowed_branches))

    # Get the debian branch
    debian_branch = utils.get_debian_branch(branch)
    origin_debian = "origin/" + debian_branch

    # Clone the repo
    repo_dir = options.repo_dir or create_temp_directory("df-repo")
    repo_dir = os.path.abspath(repo_dir)
    repo = original_repo.clone(repo_dir, branch=branch)
    print_green("Cloned repository to '%s'." % repo_dir)

    build_dir = options.build_dir or create_temp_directory("df-build")
    build_dir = os.path.abspath(build_dir)
    print_green("Build directory: '%s'" % build_dir)

    # Create the debian branch
    repo.git.branch(debian_branch, origin_debian)
    print_green("Created branch '%s' to track '%s'" % (debian_branch,
                origin_debian))

    # Go to debian branch
    repo.git.checkout(debian_branch)
    print_green("Changed to branch '%s'" % debian_branch)

    # Merge with starting branch
    repo.git.merge(branch)
    print_green("Merged branch '%s' into '%s'" % (branch, debian_branch))

    # Compute python and debian version
    cd(repo_dir)
    python_version = versioning.get_python_version()
    debian_version = versioning.\
        debian_version_from_python_version(python_version)
    print_green("The new debian version will be: '%s'" % debian_version)

    # Update the version files
    versioning.update_version()

    # Tag branch with python version
    branch_tag = python_version
    repo.git.tag(branch_tag, branch)
    upstream_tag = "upstream/" + branch_tag
    repo.git.tag(upstream_tag, branch)

    # Update changelog
    dch = git_dch("--debian-branch=%s" % debian_branch,
                  "--git-author",
                  "--ignore-regex=\".*\"",
                  "--multimaint-merge",
                  "--since=HEAD",
                  "--new-version=%s" % debian_version)
    print_green("Successfully ran '%s'" % " ".join(dch.cmd))

    if mode == "release":
        call("vim debian/changelog")
    else:
        f = open("debian/changelog", 'r+')
        lines = f.readlines()
        lines[0] = lines[0].replace("UNRELEASED", options.dist)
        lines[2] = lines[2].replace("UNRELEASED", "Snapshot build")
        f.seek(0)
        f.writelines(lines)
        f.close()

    # Add changelog to INDEX
    repo.git.add("debian/changelog")
    # Commit Changes
    repo.git.commit("-s", "-a", m="Bump version to %s" % debian_version)
    # Tag debian branch
    debian_branch_tag = "debian/" + branch_tag
    repo.git.tag(debian_branch_tag)

    # Add version.py files to repo
    call("grep \"__version_vcs\" -r . -l -I | xargs git add -f")

    # Create debian packages
    cd(repo_dir)
    version_files = []
    for _, pkg_info in config['packages'].items():
        version_files.append(pkg_info['version_file'])
    ignore_regexp = "|".join(["^(%s)$" % vf for vf in version_files])
    build_cmd = "git-buildpackage --git-export-dir=%s"\
                " --git-upstream-branch=%s --git-debian-branch=%s"\
                " --git-export=INDEX --git-ignore-new -sa"\
                " --source-option='\"--extend-diff-ignore=%s\"'"\
                " --git-upstream-tag=%s"\
                % (build_dir, branch, debian_branch, ignore_regexp,
                   upstream_tag)
    if not options.sign:
        build_cmd += " -uc -us"
    elif options.keyid:
        build_cmd += " -k\"'%s'\"" % options.keyid
    call(build_cmd)

    # Remove cloned repo
    if mode != 'release' and not options.keep_repo:
        print_green("Removing cloned repo '%s'." % repo_dir)
        rm("-r", repo_dir)

    # Print final info
    info = (("Version", debian_version),
            ("Upstream branch", branch),
            ("Upstream tag", branch_tag),
            ("Debian branch", debian_branch),
            ("Debian tag", debian_branch_tag),
            ("Repository directory", repo_dir),
            ("Packages directory", build_dir))
    print_green("\n".join(["%s: %s" % (name, val) for name, val in info]))

    # Print help message
    if mode == "release":
        origin = original_repo.remote().url
        repo.create_remote("original_origin", origin)
        print_green("Created remote 'original_origin' for the repository '%s'"
                    % origin)

        print_green("To update repositories '%s' and '%s' go to '%s' and run:"
                    % (toplevel, origin, repo_dir))
        for remote in ['origin', 'original_origin']:
            objects = [debian_branch, branch_tag, debian_branch_tag]
            print_green("git push %s %s" % (remote, " ".join(objects)))


def create_temp_directory(suffix):
    create_dir_cmd = mktemp("-d", "/tmp/" + suffix + "-XXXXX")
    return create_dir_cmd.stdout.strip()


def call(cmd):
    rc = os.system(cmd)
    if rc:
        raise RuntimeError("Command '%s' failed!" % cmd)


if __name__ == "__main__":
    sys.exit(main())
