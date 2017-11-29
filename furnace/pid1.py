#
# Copyright (c) 2006-2017 Balabit
# All Rights Reserved.
#

import json
import logging
import os
import signal
import stat
import subprocess
import sys
from socket import sethostname
from pathlib import Path

from bake.container.libc import unshare, mount, umount2, non_caching_getpid, pivot_root, \
    MS_REC, MS_SLAVE, CLONE_NEWPID, CLONE_NEWNET, MNT_DETACH
from bake.container.config import NAMESPACES, CONTAINER_MOUNTS, CONTAINER_DEVICE_NODES, HOSTNAME
from bake.logging import setup_logging

logger = logging.getLogger("container.pid1")


class PID1:
    def __init__(self, root_dir: Path, control_read, control_write, isolate_networking):
        self.control_read = control_read
        self.control_write = control_write
        self.root_dir = root_dir.resolve()
        self.isolate_networking = isolate_networking

    def enable_zombie_reaping(self):
        # We are pid 1, so we have to take care of orphaned processes
        # Interestingly, SIG_IGN is the default handler for SIGCHLD,
        # but this way we signal to the kernel that we will not call waitpid
        # and get rid of zombies automatically
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    def setup_root_mount(self):
        # SLAVE means that mount events will get inside the container, but
        # mounting something inside will not leak out.
        # Use PRIVATE to not let outside events propagate in
        mount(Path("none"), Path("/"), None, MS_REC | MS_SLAVE, None)
        old_root_dir = self.root_dir.joinpath('old_root')
        old_root_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(str(self.root_dir))
        pivot_root(Path('.'), Path('old_root'))
        os.chroot('.')

    def mount_defaults(self):
        for m in CONTAINER_MOUNTS:
            options = None
            if "options" in m:
                options = ",".join(m["options"])
            m["destination"].mkdir(parents=True, exist_ok=True)
            mount(m["source"], m["destination"], m["type"], m.get("flags", 0), options)

    def create_tmpfs_dirs(self):
        if Path('/bin/systemd-tmpfiles').exists():
            for m in CONTAINER_MOUNTS:
                if m["type"] == "tmpfs":
                    tmpfiles_output = subprocess.check_output(
                        ['/bin/systemd-tmpfiles', '--create', '--prefix', str(m["destination"])],
                        stderr=subprocess.STDOUT,
                    )
                    if tmpfiles_output:
                        logger.debug("systemd-tmpfiles output: {}".format(tmpfiles_output))
        else:
            logger.warning(
                "Could not run systemd-tmpfiles, because it does not exist. "
                "/tmp and /run will not be populated."
            )

    def create_device_node(self, name, major, minor, mode, *, is_block_device=False):
        if is_block_device:
            device_type = stat.S_IFBLK
        else:
            device_type = stat.S_IFCHR
        nodepath = Path("/dev", name)
        os.mknod(str(nodepath), mode=device_type, device=os.makedev(major, minor))
        # A separate chmod is necessary, because mknod (undocumentedly) takes umask into account when creating
        nodepath.chmod(mode=mode)

    def create_default_dev_nodes(self):
        for d in CONTAINER_DEVICE_NODES:
            self.create_device_node(d["name"], d["major"], d["minor"], 0o666)

    def create_loop_devices(self):
        self.create_device_node('loop-control', 10, 237, 0o660)
        for i in range(8):
            self.create_device_node('loop{}'.format(i), 7, i, 0o660, is_block_device=True)

    def umount_old_root(self):
        umount2('/old_root', MNT_DETACH)
        os.rmdir('/old_root')

    def create_namespaces(self):
        unshare_flags = 0
        for name, flag in NAMESPACES.items():
            if flag == CLONE_NEWPID:
                continue
            if flag == CLONE_NEWNET and not self.isolate_networking:
                continue
            if Path('/proc/self/ns', name).exists():
                unshare_flags = unshare_flags | flag
            else:
                logger.warning("Namespace type {} not supported on this system".format(name))
        unshare(unshare_flags)

    def run(self):
        if non_caching_getpid() != 1:
            raise ValueError("We are not actually PID1, exiting for safety reasons")

        # codecs are loaded dynamically, and won't work when we remount root
        make_sure_codecs_are_loaded = b'a'.decode('unicode_escape')  # NOQA: F841 local variable 'make_sure_codecs_are_loaded' is assigned to but never used
        os.setsid()
        self.enable_zombie_reaping()
        self.create_namespaces()
        self.setup_root_mount()
        self.mount_defaults()
        self.create_default_dev_nodes()
        self.create_loop_devices()
        self.create_tmpfs_dirs()
        self.umount_old_root()
        sethostname(HOSTNAME)

        os.write(self.control_write, b"RDY")
        logger.debug("Container started")
        # this will return when the pipe is closed
        # E.g. the outside control process died before killing us
        os.read(self.control_read, 1)
        logger.debug("Control pipe closed, stopping")
        return 0


if __name__ == "__main__":
    args = json.loads(sys.argv[1])
    setup_logging(args['loglevel'])
    pid1 = PID1(Path(args['root_dir']), args['control_read'], args['control_write'], args['isolate_networking'])
    sys.exit(pid1.run())
