""" Routines to copy / relink library dependencies in trees and wheels
"""

from __future__ import division, print_function

import os
from os.path import (join as pjoin, dirname, basename, exists, isdir, abspath,
                     relpath)
import shutil
import warnings
import hashlib
import csv
import glob

from wheel.util import urlsafe_b64encode, open_for_csv, native

from .libsana import tree_libs
from .tools import (add_rpath, set_install_name, zip2dir, dir2zip,
                    find_package_dirs)
from .tmpdirs import InTemporaryDirectory, InGivenDirectory

class DelocationError(Exception):
    pass


def delocate_tree_libs(lib_dict, lib_path, root_path):
    """ Move needed libraries in `lib_dict` into `lib_path`

    `lib_dict` has keys naming libraries required by the files in the
    corresponding value.  Call the keys, "required libs".  Call the values
    "requiring objects".

    Copy all the required libs to `lib_path`.  Fix up the rpaths and install
    names in the requiring objects to point to these new copies.

    Exception: required libs within the directory tree pointed to by
    `root_path` stay where they are, but we modify requiring objects to use
    relative paths to these libraries.

    Parameters
    ----------
    lib_dict : dict
        Dictionary with (key, value) pairs of (required library, set of files
        in tree depending on that library)
    lib_path : str
        Path in which to store copies of libs referred to in keys of
        `lib_dict`.  Assumed to exist
    root_path : str, optional
        Root directory of tree analyzed in `lib_dict`.  Any required
        library within the subtrees of `root_path` does not get copied, but
        libraries linking to it have links adjusted to use relative path to
        this library.

    Returns
    -------
    copied_libs : dict
        Dict with (key, value) pairs of (name of library copied into
        `lib_path`, sequence of libraries depending on copied library). The
        names are the original names from the keys of `lib_dict`
    """
    copied_libs = {}
    delocated_libs = set()
    copied_basenames = set()
    # Test for errors first to avoid getting half-way through changing the tree
    for required, requirings in lib_dict.items():
        if required.startswith('@'): # assume @rpath etc are correct
            # But warn, because likely they are not
            warnings.warn('Not processing required path {0} because it '
                          'begins with @'.format(required))
            continue
        r_ed_base = basename(required)
        if relpath(required, root_path).startswith('..'):
            # Not local, plan to copy
            if r_ed_base in copied_basenames:
                raise DelocationError('Already planning to copy library with '
                                      'same basename as: ' + r_ed_base)
            if not exists(required):
                raise DelocationError('library "{0}" does not exist'.format(
                    required))
            copied_libs[required] = requirings
            copied_basenames.add(r_ed_base)
        else: # Is local, plan to set relative loader_path
            delocated_libs.add(required)
    # Modify in place now that we've checked for errors
    for required in copied_libs:
        shutil.copy2(required, lib_path)
        # Set rpath and install names for this copied library
        for requiring in lib_dict[required]:
            req_rel = relpath(lib_path, dirname(requiring))
            set_install_name(requiring, required,
                             '@loader_path/{0}/{1}'.format(
                                 req_rel, basename(required)))
    for required in delocated_libs:
        # Set relative path for local library
        for requiring in lib_dict[required]:
            req_rel = relpath(required, dirname(requiring))
            set_install_name(requiring, required,
                             '@loader_path/' + req_rel)
    return copied_libs


def copy_recurse(lib_path, copy_filt_func = None, copied_libs = None):
    """ Analyze `lib_path` for library dependencies and copy libraries

    `lib_path` is a directory containing libraries.  The libraries might
    themselves have dependencies.  This function analyzes the dependencies and
    copies library dependencies that match the filter `copy_filt_func`. It alos
    adjusts the depending libraries to use the copy. It keeps iterating over
    `lib_path` until all matching dependencies (of dependencies of
    dependencies ...) have been copied.

    Parameters
    ----------
    lib_path : str
        Directory containing libraries
    copy_filt_func : None or callable, optional
        If None, copy any library that found libraries depend on.  If callable,
        called on each depended library name; copy where
        ``copy_filt_func(libname)`` is True, don't copy otherwise
    copied_libs : dict
        Dict with (key, value) pairs of (name of library copied into
        `lib_path`, sequence of libraries depending on copied library). The
        names are the original names from the keys of `lib_dict`

    Returns
    -------
    copied_libs : dict
        Input `copied_libs` dict with any extra libraries and / or dependencies
        added.
    """
    if copied_libs is None:
        copied_libs = {}
    else:
        copied_libs = dict(copied_libs)
    done = False
    while not done:
        in_len = len(copied_libs)
        _copy_required(lib_path, copy_filt_func, copied_libs)
        done = len(copied_libs) == in_len
    return copied_libs


def _copy_required(lib_path, copy_filt_func, copied_libs):
    """ Copy libraries required for files in `lib_path` to `lib_path`

    Augment `copied_libs` dictionary with any newly copied libraries, modifying
    `copied_libs` in-place.

    This is one pass of ``copy_recurse``

    Parameters
    ----------
    lib_path : str
        Directory containing libraries
    copy_filt_func : None or callable, optional
        If None, copy any library that found libraries depend on.  If callable,
        called on each library name; copy where ``copy_filt_func(libname)`` is
        True, don't copy otherwise
    copied_libs : dict
        Dict with (key, value) pairs of (name of library copied into
        `lib_path`, sequence of libraries depending on copied library). The
        names are the original names from the keys of `lib_dict`
    """
    # Paths will be prepended with `lib_path`
    lib_dict = tree_libs(lib_path)
    # Map library paths after copy to path before copy
    copied2orig = dict((pjoin(lib_path, basename(c)), c) for c in copied_libs)
    for required, requirings in lib_dict.items():
        if not copy_filt_func is None and not copy_filt_func(required):
            continue
        if required.startswith('@'):
            # May have been processed by us, or have some rpath, loader_path of
            # its own. Either way, leave alone
            continue
        required_base = basename(required)
        # Will we need to make a local copy or do we have this one already?
        needs_copy = required not in copied_libs
        # Set requiring lib install names to point to local copy
        for requiring in requirings:
            set_install_name(requiring,
                             required,
                             '@loader_path/' + required_base)
        # Make requiring names point to original libraries
        procd_requirings = set(copied2orig.get(r, r) for r in requirings)
        if needs_copy: # Haven't see this one before, add entry to copied_libs
            shutil.copy2(required, lib_path)
            copied2orig[pjoin(lib_path, required_base)] = required
            copied_libs[required] = procd_requirings
        else: # Have seen this before, add any new requirements
            copied_libs[required] = copied_libs[required] | procd_requirings


def _dylibs_only(filename):
    return (filename.endswith('.so') or
            filename.endswith('.dylib'))


def filter_system_libs(libname):
    return not (libname.startswith('/usr/lib') or
                libname.startswith('/System'))


def delocate_path(tree_path, lib_path,
                  lib_filt_func = _dylibs_only,
                  copy_filt_func = filter_system_libs):
    """ Copy required libraries for files in `tree_path` into `lib_path`

    Parameters
    ----------
    tree_path : str
        Root path of tree to search for required libraries
    lib_path : str
        Directory into which we copy required libraries
    lib_filt_func : None or callable, optional
        If None, inspect all files for dependencies on dynamic libraries. If
        callable, accepts filename as argument, returns True if we should
        inspect the file, False otherwise. Default is callable rejecting all
        but files ending in ``.so`` or ``.dylib``.
    copy_filt_func : None or callable, optional
        If callable, called on each library name detected as a dependency; copy
        where ``copy_filt_func(libname)`` is True, don't copy otherwise.
        Default is callable rejecting only libraries beginning with
        ``/usr/lib`` or ``/System``.  None means copy all libraries. This will
        usually end up copying large parts of the system run-time.

    Returns
    -------
    copied_libs : dict
        Dict with (key, value) pairs of (name of library copied into
        `lib_path`, sequence of libraries depending on copied library). The
        names are the original names from the keys of `lib_dict`
    """
    if not exists(lib_path):
        os.makedirs(lib_path)
    lib_dict = tree_libs(tree_path, lib_filt_func)
    if not copy_filt_func is None:
        lib_dict = dict((key, value) for key, value in lib_dict.items()
                        if copy_filt_func(key))
    copied = delocate_tree_libs(lib_dict, lib_path, tree_path)
    return copy_recurse(lib_path, copy_filt_func, copied)


def rewrite_record(bdist_dir):
    """ Rewrite RECORD file with hashes for all files in `wheel_sdir`

    Copied from :method:`wheel.bdist_wheel.bdist_wheel.write_record`

    Will also unsign wheel

    Parameters
    ----------
    bdist_dir : str
        Path of unpacked wheel file
    """
    info_dirs = glob.glob(pjoin(bdist_dir, '*.dist-info'))
    if len(info_dirs) != 1:
        raise DelocationError("Should be exactly one `*.dist_info` directory")
    record_path = os.path.join(info_dirs[0], 'RECORD')
    record_relpath = os.path.relpath(record_path, bdist_dir)
    # Unsign wheel - because we're invalidating the record hash
    sig_path = os.path.join(info_dirs[0], 'RECORD.jws')
    if exists(sig_path):
        os.unlink(sig_path)

    def walk():
        for dir, dirs, files in os.walk(bdist_dir):
            for f in files:
                yield os.path.join(dir, f)

    def skip(path):
        """Wheel hashes every possible file."""
        return (path == record_relpath)

    with open_for_csv(record_path, 'w+') as record_file:
        writer = csv.writer(record_file)
        for path in walk():
            relpath = os.path.relpath(path, bdist_dir)
            if skip(relpath):
                hash = ''
                size = ''
            else:
                with open(path, 'rb') as f:
                    data = f.read()
                digest = hashlib.sha256(data).digest()
                hash = 'sha256=' + native(urlsafe_b64encode(digest))
                size = len(data)
            record_path = os.path.relpath(
                path, bdist_dir).replace(os.path.sep, '/')
            writer.writerow((record_path, hash, size))


def _merge_lib_dict(d1, d2):
    for required, requirings in d2.items():
        if required in d1:
            d1[required] = d1[required] | requirings
        else:
            d1[required] = requirings
    return 


def delocate_wheel(in_wheel,
                   out_wheel = None,
                   lib_sdir = '.dylibs',
                   lib_filt_func = _dylibs_only,
                   copy_filt_func = filter_system_libs):
    """ Update wheel by copying required libraries to `lib_sdir` in wheel

    Create `lib_sdir` in wheel tree only if we are copying one or more
    libraries.

    Overwrite the wheel `wheel_fname` in-place.

    Parameters
    ----------
    in_wheel : str
        Filename of wheel to process
    out_wheel : None or str
        Filename of processed wheel to write.  If None, overwrite `in_wheel`
    lib_sdir : str, optional
        Subdirectory name in wheel package directory (or directories) to store
        needed libraries.
    lib_filt_func : None or callable, optional
        If None, inspect all files for dependencies on dynamic libraries. If
        callable, accepts filename as argument, returns True if we should
        inspect the file, False otherwise. Default is callable rejecting all
        but files ending in ``.so`` or ``.dylib``.
    copy_filt_func : None or callable, optional
        If callable, called on each library name detected as a dependency; copy
        where ``copy_filt_func(libname)`` is True, don't copy otherwise.
        Default is callable rejecting only libraries beginning with
        ``/usr/lib`` or ``/System``.  None means copy all libraries. This will
        usually end up copying large parts of the system run-time.

    Returns
    -------
    copied_libs : dict
        Dict with (key, value) pairs of (name of library copied into
        `lib_path`, sequence of libraries depending on copied library). The
        names are the original names from the keys of `lib_dict`
    """
    in_wheel = abspath(in_wheel)
    if out_wheel is None:
        out_wheel = in_wheel
    else:
        out_wheel = abspath(out_wheel)
    in_place = in_wheel == out_wheel
    with InTemporaryDirectory():
        all_copied = {}
        zip2dir(in_wheel, 'wheel')
        with InGivenDirectory('wheel'):
            for package_path in find_package_dirs('.'):
                lib_path = pjoin(package_path, lib_sdir)
                lib_path_exists = exists(lib_path)
                copied_libs = delocate_path(package_path, lib_path,
                                            lib_filt_func, copy_filt_func)
                if copied_libs and lib_path_exists:
                    raise DelocationError(
                        '{0} already exists in wheel'.format(lib_path))
                if len(os.listdir(lib_path)) == 0:
                    shutil.rmtree(lib_path)
                _merge_lib_dict(all_copied, copied_libs)
        if len(all_copied):
            rewrite_record('wheel')
        if len(all_copied) or not in_place:
            dir2zip('wheel', out_wheel)
    return all_copied