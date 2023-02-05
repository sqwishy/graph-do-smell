#!/usr/bin/env python3
r"""
This is a program to create an lvm snapshot and mount it into the mount
namespace of another program. This creates a unix socket that the other
program can connect over to make request for a mount.

When a mount request is made, it can include tags to be specific about what
logical volume should be taken a snapshot of. It can also include tags about
the logical volume being made so it can be referenced by later requests.

Requests are a super basic text "protocol" which looks like:

    mount /some/mount/path
    > tags separated by whitespace
    < tags separated by whitespace

Each line prefixed with `>` and `<` can occur multiple times.

Tags listed after `>` are matched against existing logical volumes to
find a candidate for a new snapshot. *All* tags listed in a line must be
present for a logical volume to match. The order of tags in the same line
does not matter, but if multiple `>` lines are given they are handled in order.
After a match is found, any further `>` lines are ignored. So list prefered or
more specific groups of tags first. Logical volumes on the system are iterated
by creation time with most recent first.

Tags listed after `<` are used to tag the logical volume created for the mount.
This to can occur multiple times and all tags listed in each occurrence are
applied.

For example [1]:

    ncat -U /run/lvm-cache-friend/socket <<EOF
    mount /some/place
    > my-application hash-of-dependencies
    > my-application
    < my-application hash-of-dependencies
    EOF

Will first look for the most recent logical volume with both "my-application"
and "hash-of-dependencies" tags (because of the first `>`).

If none were found, it will then look for the most recent logical volume with
just the "my-application" tag (from of the second `>`).

If that doesn't find anything, the logical volume specified with `--default`
will be used for the snapshot.

The `mount` request can be issued multiple times in a single connection as long
as blank line separates each.

When a snapshot is taken, the new logical volume is immediately tagged. This
means it can be used for a new snapshot even if it's still mounted and in use.
I think, in that case, if you try to mount the new snapshot it will fail.
Because lvm doesn't let you mount either a snapshot if its origin is mounted,
or an origin if a snapshot of it is mounted. But I don't know enough about lvm
to say for sure. Anyway, this is intended to be used by just one other program
that mounts one volume at a time.

Example service file:

    [Service]
    Type=simple
    ExecStart=/usr/local/bin/lvm-cache-friend.py
    Restart=on-failure

    [Install]
    WantedBy=multi-user.target

To create a thin pool.

    lvcreate \
        --type thin-pool \
        --size 30G \
        my-volume-group/friend-pool

To create a thin logical volume in tha pool.

    lvcreate \
        --thinpool my-volume-roup/friend-pool \
        --virtualsize 3G \
        --name friend-default \
        --addtag friend:default

The friend:default tag corresponds to the --default option for this program.

[1] Note: ncat is a nice client for this because, by default, it seems to wait
    for the server to close the connection before it exits. The server will
    close the connection after handling requests and reading EOF from the
    client. This is important so that ncat exits after the mount has been set
    up (or at least attempted).

    It's also probably worth noting that there's no explicit way to determine if
    the mount worked. It's kinda up to you to maybe checking the filesystem
    after ncat quits or something. The server should copy errors to the client
    for debugging, but the exit code is not sent.
"""

from sys import stderr
from os import unlink, chmod, environ, mkdir
from os.path import dirname
from operator import itemgetter
from json import loads, JSONDecodeError
from subprocess import run as subprocess_run, CalledProcessError
from socket import socket, AF_UNIX, SO_PEERCRED
from dataclasses import dataclass
from time import time
from struct import Struct
from random import randrange
from base64 import urlsafe_b64encode
import re


if stderr.isatty() and "NO_COLOR" not in environ:
    ANSI_RESET = "\x1b[0m"
    ANSI_RED = "\x1b[38;5;9m"
    ANSI_YELLOW = "\x1b[38;5;11m"
    ANSI_MAGENTA = "\x1b[38;5;13m"
    ANSI_TEAL = "\x1b[38;5;14m"
else:
    ANSI_RESET = ANSI_RED = ANSI_YELLOW = ANSI_MAGENTA = ANSI_TEAL = ""


if "JOURNAL_STREAM" in environ:
    # https://www.freedesktop.org/software/systemd/man/sd-daemon.html
    PREFIX_OOPS = "<3>"  # <3> looks red
    PREFIX_WARN = "<4>"  # <4> looks kinda yellow, <5> is bold
    PREFIX_INFO = "<6>"  # <6> normal
    PREFIX_SUBP = "<7>"  # <7> grey like commented out
else:
    PREFIX_OOPS = PREFIX_WARN = PREFIX_INFO = PREFIX_SUBP = ""


def _log(level, *args, **extra):
    extra = "".join(f"\n     {k} ➭ {v}" for k, v in extra.items() if v != "")
    print(level, *args, extra, file=stderr)


def log_oops(*args, **extra):
    _log(f"{PREFIX_OOPS}{ANSI_RED}oops{ANSI_RESET}", *args, **extra)


def log_warn(*args, **extra):
    _log(f"{PREFIX_WARN}{ANSI_YELLOW}warn{ANSI_RESET}", *args, **extra)


def log_info(*args, **extra):
    _log(f"{PREFIX_INFO}{ANSI_TEAL}info{ANSI_RESET}", *args, **extra)


def log_subp(*args, **extra):
    _log(f"{PREFIX_SUBP}{ANSI_MAGENTA}subp{ANSI_RESET}", *args, **extra)


def ritemgetter(*items):
    def inner(v):
        for item in items:
            v = v[item]
        return v

    return inner


def eq(lh):
    return lambda rh: lh == rh


def startswith(prefix):
    return lambda s: s.startswith(prefix)


def drop_prefix(text, prefix):
    if text.startswith(prefix):
        return text[len(prefix) :].strip()


# Characters allowed in tags are: A-Z a-z 0-9 _ + . - and as of
# version 2.02.78 the following characters are also accepted: / = ! : # &
TAG_PATTERN = re.compile(r"[^a-zA-Z0-9/=!:\#\&\+\.\-_]")


def clean_tag(text):
    """
    >>> clean_tag("a-zA-Z0-9/=!:#&+.-_")
    'a-zA-Z0-9/=!:#&+.-_'
    >>> clean_tag("foo?")
    'foo-'
    >>> clean_tag("@% bar")
    '---bar'
    >>> clean_tag("[foo]*{bar}")
    '-foo---bar-'
    """
    return TAG_PATTERN.sub("-", text)


def run(*argv):
    completed = subprocess_run(argv, capture_output=True, encoding="utf8")
    completed.check_returncode()  # may raise CalledProcessError
    log_subp(*argv, stderr=completed.stderr)
    return completed


def run_noraise(*argv):
    try:
        return run(*argv)
    except CalledProcessError as err:
        log_warn(*argv, code=err.returncode, stdout=err.stdout, stderr=err.stderr)


def unix_listen(path):
    try:
        unlink(path)
    except FileNotFoundError:
        pass

    sock = socket(family=AF_UNIX)
    sock.bind(path)

    chmod(path, 0o777)

    sock.listen()

    return sock


@dataclass
class Load(object):
    dst: str
    addtags: list[str]
    findtags: list[list[str]]


def peer_requests(sock, *, socket_timeout):
    """yields pid, dst, tags"""
    try:
        peer, _ = sock.accept()
    except KeyboardInterrupt:
        raise SystemExit(0)

    peer_pid = peer.getsockopt(AF_UNIX, SO_PEERCRED)
    peer.settimeout(socket_timeout)

    log_info("connected", pid=peer_pid)

    try:
        with peer.makefile(mode="rw") as buf:
            for req in read_peer(buf):
                yield buf, peer_pid, req
    except ConnectionError as e:
        log_warn("peer connection error", exception=e, pid=peer_pid)
    except TimeoutError as e:
        log_warn("peer timed out", pid=peer_pid)


def read_peer(buf):
    while line := buf.readline().strip():

        if (dst := drop_prefix(line, "mount")) is not None:
            addtags = []
            findtags = []

            for line in read_until_empty_line(buf):
                if addtag := drop_prefix(line, "<"):
                    addtags.extend(addtag.split())

                elif findtag := drop_prefix(line, ">"):
                    findtags.append(findtag.split())

                else:
                    print(f"ignoring unexpected `{line}`", file=buf)

            yield Load(dst, addtags=addtags, findtags=findtags)

        elif (rest := drop_prefix(line, "bye")) is not None:
            print(f"glhf", rest, file=buf)
            break

        else:
            print(f"unexpected `{line}`", file=buf)
            break


def read_until_empty_line(buf):
    while line := buf.readline().strip():
        yield line


_snapshottobytes = Struct(">IH")


def next_snapshot_name():
    """return string base64 encoded 32 bits unix timestamp + 16 bits random"""
    name = _snapshottobytes.pack(int(time()), randrange(2**16))
    return urlsafe_b64encode(name).decode()


@dataclass
class Lv(object):
    vg: str
    name: str
    tags: list[str]


def lv_has_tag(test):
    return lambda lv: any(map(test, lv.tags))


class LvsError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.extra = kwargs


_lvs_options = "vg_name,lv_name,lv_tags"
_lvs_astuple = itemgetter(*_lvs_options.split(","))
_lvs_path = ritemgetter("report", 0, "lv")
# fmt: off
_lvs_argv = [
    "lvs",
    "--sort", "-lv_time",
    "--options", _lvs_options,
    # newer versions have a json_std format but I don't have that on fedora yet
    "--reportformat", "json",
]
# fmt: on


def iter_lvs():
    """This is an iterator and _could_ raise LvsError while iterating."""
    try:
        completed = run(*_lvs_argv)
    except CalledProcessError as err:
        raise LvsError(
            *err.cmd, code=err.returncode, stdout=err.stdout, stderr=err.stderr
        )

    try:
        parsed = loads(completed.stdout)
    except JSONDecodeError as err:
        raise LvsError("failed to parse lvs as json", err=err, output=completed.stdout)

    try:
        for vg, name, tags in map(_lvs_astuple, _lvs_path(parsed)):
            yield Lv(vg=vg, name=name, tags=tags.split(","))
    except KeyError as err:
        raise LvsError("unexpected structure from lvs", err=err, output=parsed)


def lvcreate_snapshot(vg, from_lv, name, tags=()):
    # fyi lvcreate --reportformat json doesn't seem to work with --snapshot :(
    addtags = (arg for tag in tags for arg in ("--addtag", tag))
    # fmt: off
    return run("lvcreate",
               "--snapshot", f"{vg}/{from_lv}",
               "--ignoreactivationskip",
               "--name", name, *addtags)
    # fmt: on


def make_stage_dirs_under(top):
    stage = f"{top}/stage"
    inner = f"{top}/stage/inner"

    try:
        mkdir(stage)
    except FileExistsError:
        pass
    else:
        log_info("created stage", path=stage)

    try:
        mkdir(inner)
    except FileExistsError:
        pass
    else:
        log_info("created stage/inner", path=inner)


def mount_into_namespace(top, vg, lv, pid, dst, mount_options):
    """
    Based on my rough understanding of
    https://github.com/systemd/systemd/blob/v252/src/shared/mount-util.c#L780
    """

    stage = f"{top}/stage"
    inner = f"{top}/stage/inner"
    mount_options = ("-o", mount_options) if mount_options else ()

    run("mount", "--bind", stage, stage)
    try:
        run("mount", *mount_options, f"/dev/{vg}/{lv}", inner)
        try:
            run("mount", "--namespace", str(pid), "--make-private", stage)
            try:
                run("mount", "--namespace", str(pid), "--move", inner, dst)
            except CalledProcessError:
                # only attempt unmount stage/inner in namespace
                # if it was not --move'd
                run_noraise("umount", "--namespace", str(pid), inner)
                raise
            finally:
                run_noraise("umount", "--namespace", str(pid), stage)
        finally:
            run_noraise("umount", inner)
    finally:
        run_noraise("umount", stage)


def main():
    from argparse import (
        ArgumentParser,
        RawDescriptionHelpFormatter,
        ArgumentDefaultsHelpFormatter,
    )

    class formatter_class(RawDescriptionHelpFormatter, ArgumentDefaultsHelpFormatter):
        pass

    parser = ArgumentParser(formatter_class=formatter_class, epilog=__doc__)
    # fmt: off
    parser.add_argument("--socket", default="/run/lvm-cache-friend/socket", help="parent directory is expected to already exist; the socket is removed if it exists when this program starts")
    parser.add_argument("--default", default="friend:default", help="tag for default logical volume to make a snapshot from", type=clean_tag)
    parser.add_argument("--tag-prefix", default="friend:cache:", help="tag prefix used when searching or adding tags from the mount command, used for namespacing", type=clean_tag)
    parser.add_argument("--tag-snapshot", default="friend:snapshot", help="bonus tag added to snapshots when this program creates them with lvcreate (does not include --tag-prefix)", type=clean_tag)
    parser.add_argument("--name-prefix", default="friend-", help="snapshots are named with this prefix")
    parser.add_argument("--timeout", default=2.5, help="socket timeout in seconds")
    parser.add_argument("--mount-options", default="discard", help="passed to `mount -o ...` when mounting the logical volume")
    # fmt: on
    args = parser.parse_args()

    try:
        for default in filter(lv_has_tag(eq(args.default)), iter_lvs()):
            log_info("default lv", default.vg, default.name)
            break
        else:
            log_oops(f"could not find default lv (tagged `{args.default}`)")
            raise SystemExit(1)
    except LvsError as err:
        log_oops(*err.args, **err.extra)
        raise SystemExit(1)

    sock = unix_listen(args.socket)
    log_info("listening on", args.socket)

    shared = dirname(args.socket)
    make_stage_dirs_under(shared)

    tag_snapshot = [clean_tag(args.tag_snapshot)] if args.tag_snapshot else []

    while True:
        for peer, pid, mount in peer_requests(sock, socket_timeout=args.timeout):

            log_info(mount)

            try:
                lvs = list(iter_lvs())
            except LvsError as err:
                log_oops(*err.args, **err.extra)
                break  # disconnect from peer

            for tags in mount.findtags:
                required = [args.tag_prefix + clean_tag(tag) for tag in tags]
                hasall = lambda lv: all(tag in lv.tags for tag in required)
                if lv := next(filter(hasall, lvs), None):
                    log_info("matched", lv=lv)
                    break
            else:
                lv = default
                log_info("no match, using default")

            snapshot_name = args.name_prefix + next_snapshot_name()
            addtags = [
                args.tag_prefix + clean_tag(tag) for tag in mount.addtags
            ] + tag_snapshot

            try:
                lvcreate_snapshot(lv.vg, lv.name, snapshot_name, addtags)
                mount_into_namespace(
                    shared, lv.vg, snapshot_name, pid, mount.dst, args.mount_options
                )
            except CalledProcessError as err:
                log_oops(
                    *err.cmd,
                    code=err.returncode,
                    stdout=err.stdout,
                    stderr=err.stderr,
                )
                print(err.stderr, file=peer)
            else:
                print(lv.vg, lv.name, file=peer)

    raise SystemExit(0)


if __name__ == "__main__":
    main()
