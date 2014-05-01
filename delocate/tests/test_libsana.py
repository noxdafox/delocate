""" Tests for libsana module

Utilities for analyzing library dependencies in trees and wheels
"""

import os
from os.path import join as pjoin, split as psplit, abspath, dirname, realpath

from ..libsana import tree_libs, wheel_libs
from ..tools import set_install_name

from ..tmpdirs import InTemporaryDirectory

from nose.tools import (assert_true, assert_false, assert_raises,
                        assert_equal, assert_not_equal)

from .test_install_names import (LIBA, LIBB, LIBC, TEST_LIB, _copy_libs,
                                 EXT_LIBS)
from .test_wheelies import PLAT_WHEEL, PURE_WHEEL, STRAY_LIB_DEP


def get_ext_dict(local_libs):
    ext_deps = {}
    for ext_lib in EXT_LIBS:
        lib_deps = {}
        for local_lib in local_libs:
            lib_deps[realpath(local_lib)] = ext_lib
        ext_deps[realpath(ext_lib)] = lib_deps
    return ext_deps


def test_tree_libs():
    # Test ability to walk through tree, finding dynamic libary refs
    # Copy specific files to avoid working tree cruft
    to_copy = [LIBA, LIBB, LIBC, TEST_LIB]
    with InTemporaryDirectory() as tmpdir:
        local_libs = _copy_libs(to_copy, tmpdir)
        rp_local_libs = [realpath(L) for L in local_libs]
        liba, libb, libc, test_lib = local_libs
        rp_liba, rp_libb, rp_libc, rp_test_lib = rp_local_libs
        exp_dict = get_ext_dict(local_libs)
        exp_dict.update({
             rp_liba: {rp_libb: 'liba.dylib', rp_libc: 'liba.dylib'},
             rp_libb: {rp_libc: 'libb.dylib'},
             rp_libc: {rp_test_lib: 'libc.dylib'}})
        # default - no filtering
        assert_equal(tree_libs(tmpdir), exp_dict)
        def filt(fname):
            return fname.endswith('.dylib')
        exp_dict = get_ext_dict([liba, libb, libc])
        exp_dict.update({
             rp_liba: {rp_libb: 'liba.dylib', rp_libc: 'liba.dylib'},
             rp_libb: {rp_libc: 'libb.dylib'}})
        # filtering
        assert_equal(tree_libs(tmpdir, filt), exp_dict)
        # Copy some libraries into subtree to test tree walking
        subtree = pjoin(tmpdir, 'subtree')
        slibc, stest_lib = _copy_libs([libc, test_lib], subtree)
        st_exp_dict = get_ext_dict([liba, libb, libc, slibc])
        st_exp_dict.update({
            rp_liba: {rp_libb: 'liba.dylib',
                      rp_libc: 'liba.dylib',
                      realpath(slibc): 'liba.dylib'},
            rp_libb: {rp_libc: 'libb.dylib',
                      realpath(slibc): 'libb.dylib'}})
        assert_equal(tree_libs(tmpdir, filt), st_exp_dict)
        # Change an install name, check this is picked up
        set_install_name(slibc, 'liba.dylib', 'newlib')
        inc_exp_dict = get_ext_dict([liba, libb, libc, slibc])
        inc_exp_dict.update({
            rp_liba: {rp_libb: 'liba.dylib',
                      rp_libc: 'liba.dylib'},
            realpath('newlib'): {realpath(slibc): 'newlib'},
            rp_libb: {rp_libc: 'libb.dylib',
                      realpath(slibc): 'libb.dylib'}})
        assert_equal(tree_libs(tmpdir, filt), inc_exp_dict)
        # Symlink a depending canonical lib - should have no effect because of
        # the canonical names
        os.symlink(liba, pjoin(dirname(liba), 'funny.dylib'))
        assert_equal(tree_libs(tmpdir, filt), inc_exp_dict)
        # Symlink a depended lib.  Now 'newlib' is a symlink to liba, and the
        # dependency of slibc on newlib appears as a dependency on liba, but
        # with install name 'newlib'
        os.symlink(liba, 'newlib')
        sl_exp_dict = get_ext_dict([liba, libb, libc, slibc])
        sl_exp_dict.update({
            rp_liba: {rp_libb: 'liba.dylib',
                      rp_libc: 'liba.dylib',
                      realpath(slibc): 'newlib'},
            rp_libb: {rp_libc: 'libb.dylib',
                      realpath(slibc): 'libb.dylib'}})
        assert_equal(tree_libs(tmpdir, filt), sl_exp_dict)


def test_wheel_libs():
    # Test routine to list dependencies from wheels
    assert_equal(wheel_libs(PURE_WHEEL), {})
    mod2 = pjoin('fakepkg1', 'subpkg', 'module2.so')
    rp_stray = realpath(STRAY_LIB_DEP)
    rp_mod2 = realpath(mod2)
    sys_b = '/usr/lib/libSystem.B.dylib'
    assert_equal(wheel_libs(PLAT_WHEEL),
                 {rp_stray: {rp_mod2: STRAY_LIB_DEP},
                  realpath(sys_b): {rp_mod2: sys_b}})
    def filt(fname):
        return not fname == mod2
    assert_equal(wheel_libs(PLAT_WHEEL, filt), {})