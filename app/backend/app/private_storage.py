"""Private product storage without changing user-selected ancestor permissions."""
import os
from pathlib import Path
import stat
import sys


def _private_mode(fd: int, mode: int) -> None:
    os.fchmod(fd, mode)
    if sys.platform != 'darwin':
        return
    # macOS mode bits do not remove extended ACL grants. Work on the already
    # ownership-checked descriptor, never a path that could be replaced.
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.acl_init.argtypes = [ctypes.c_int]
    libc.acl_init.restype = ctypes.c_void_p
    libc.acl_set_fd.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.acl_set_fd.restype = ctypes.c_int
    libc.acl_free.argtypes = [ctypes.c_void_p]
    libc.acl_free.restype = ctypes.c_int
    acl = libc.acl_init(0)
    if not acl:
        raise OSError(ctypes.get_errno(), 'Cannot initialize private storage ACL')
    try:
        if libc.acl_set_fd(fd, acl) != 0:
            raise OSError(ctypes.get_errno(), 'Cannot secure private storage ACL')
    finally:
        libc.acl_free(acl)


def check_directory_path(path: Path) -> None:
    current = path.expanduser().absolute()
    for node in (current, *current.parents):
        if node.is_symlink():
            system_alias = (sys.platform == 'darwin' and node in (Path('/var'), Path('/tmp'))
                            and node.resolve() == Path('/private') / node.name)
            if not system_alias:
                raise OSError('Product data path cannot contain symbolic links')


def private_directory(path: Path) -> None:
    check_directory_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError('Product data directory must be an owned directory, not a link')
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise OSError('Product data directory ownership changed')
        _private_mode(fd, 0o700)
    finally:
        os.close(fd)


def private_file(path: Path, *, create: bool = False) -> None:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError('Product data file must be a singly linked, owned regular file')
        if not create and info.st_nlink == 0:
            return
        if info.st_nlink != 1:
            raise OSError('Product data file must be a singly linked, owned regular file')
        _private_mode(fd, 0o600)
    finally:
        os.close(fd)


def verify_database_stat(path: Path, *, data_directory: Path | None = None) -> None:
    """Validate ownership/regularity via lstat only, without opening or closing FDs.

    Closing any descriptor for an inode on POSIX drops all fcntl advisory locks
    held by the current process (including SQLite's -shm locks held by _WalKeeperHandle).
    """
    check_directory_path(path.parent)
    if data_directory is not None and path.parent.absolute() == data_directory.absolute():
        dir_info = path.parent.lstat()
        if not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != os.getuid():
            raise OSError('Product data directory must be an owned directory, not a link')
    for suffix in ('', '-wal', '-shm', '-journal'):
        candidate = Path(str(path) + suffix) if suffix else path
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if not suffix:
                raise
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError('Product data file must be a singly linked, owned regular file')
        if info.st_nlink != 1 and not (suffix and info.st_nlink == 0):
            raise OSError('Product data file must be a singly linked, owned regular file')


def prepare_database(path: Path, *, data_directory: Path | None = None) -> None:
    # Only the explicitly designated product data leaf is migrated. A caller
    # supplying an arbitrary DB file does not grant ownership of its parent.
    check_directory_path(path.parent)
    if data_directory is not None and path.parent.absolute() == data_directory.absolute():
        private_directory(path.parent)
    else:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for suffix in ('-wal', '-shm', '-journal'):
        private_file(Path(str(path) + suffix))
    private_file(path, create=True)
