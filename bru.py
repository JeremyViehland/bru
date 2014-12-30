#!/usr/bin/env python3

import json
import itertools
import collections
import urllib.request
import urllib.parse # python 2 urlparse
import re
import os
import os.path
import platform
import shutil
import subprocess
import glob
import time
import argparse
from enum import Enum
import pdb # only if you want to add pdb.set_trace()

import brulib.jsonc
import brulib.library
import brulib.untar
import brulib.clone

# http://stackoverflow.com/questions/4934806/python-how-to-find-scripts-directory
def get_script_path():
    return os.path.dirname(os.path.realpath(__file__))

def get_library_dir():
    """ assuming we execute bru.py from within its git clone the library
        directory will be located in bru.py's base dir. This func here
        returns the path to this library dir. """
    return os.path.join(get_script_path(), 'library')

def get_library():
    return brulib.library.Library(get_library_dir())

def get_user_home_dir():
    """ work both on Linux & Windows, this dir will be the parent dir of
        the .bru/ dir for storing downloaded tar.gzs on a per-user basis"""
    return os.path.expanduser("~")

# from http://stackoverflow.com/questions/431684/how-do-i-cd-in-python
class Chdir:
    """ Context manager for changing the current working directory.
        Used in conjunction with os.system for executing $make_command,
        typically used to run ./configure
    """
    def __init__( self, newPath ):
        self.newPath = newPath

    def __enter__(self):
        self.savedPath = os.getcwd()
        os.chdir(self.newPath)

    def __exit__(self, etype, value, traceback):
        os.chdir(self.savedPath)

def unpack_dependency(bru_modules_root, module_name, module_version, zip_url):
    """ downloads tar.gz or zip file as given by zip_url, then unpacks it
        under bru_modules_root """
    src_module_dir = get_library().get_module_dir(module_name)
    module_dir = os.path.join(bru_modules_root, module_name, module_version)
    os.makedirs(module_dir, exist_ok=True)

    parse = urllib.parse.urlparse(zip_url)
    if parse.scheme in ['svn+http', 'svn+https','git+http', 'git+https']:
        brulib.clone.atomic_clone_repo(zip_url, module_dir)
        return

    if parse.scheme == 'file':
        # this is typically used to apply a patch in the form of a targ.gz
        # on top of a larger downloaded file. E.g. for ogg & speex this
        # patch does approximately what ./configure would have done.
        # copying the tar file itself from ./library to ./modules would be
        # pointless, so we extract this file right from the library dir:
        path = parse.netloc
        assert len(path) > 0
        basename = os.path.basename(path)
        assert len(path) > 0
        src_tar_filename = os.path.join(src_module_dir, path)
        brulib.untar.untar_once(src_tar_filename, module_dir)
        return

    # Store all downloaded tar.gz files in ~/.bru, e.g as boost-regex/1.57/foo.tar.gz
    # This ensures that multiple 'bru install foo' cmds in differet directories
    # on this machine won't download the same foo.tar.gz multiple times.
    # MOdules for which we must clone an svn or git repo are not sharable that
    # easily btw, they actually are cloned multiple times atm (could clone them
    # once into ~/.bru and copy, but I'm not doing this atm).
    if parse.scheme in ['http', 'https', 'ftp']:
        tar_dir = os.path.join(get_user_home_dir(), ".bru", "downloads",
                               module_name, module_version)
        brulib.untar.wget_and_untar_once(zip_url, tar_dir, module_dir)
        return

    raise Exception('unsupported scheme in', zip_url)

def unpack_module(formula):
    if not 'module' in formula or not 'version' in formula:
        print(json.dumps(formula, indent=4))
        raise Exception('missing module & version')
    module = formula['module']
    version = formula['version']
    zip_urls = formula['url']

    # 'url' can be a single string or a list
    if isinstance(zip_urls, str):
        zip_urls = [zip_urls]

    bru_modules_root = './bru_modules'
    for zip_url in zip_urls:
        unpack_dependency(bru_modules_root, module, version, zip_url)
    module_dir = os.path.join(bru_modules_root, module, module)
    return module_dir

def get_dependency(module_name, module_version):
    bru_modules_root = "./bru_modules"
    formula = get_library().load_formula(module_name, module_version)
    unpack_module(formula)

    # make_command should only be used if we're too lazy to provide a
    # gyp file for a module.
    # A drawback of using ./configure make is that build are less reproducible
    # across machines, e.g. ./configure may enable some code paths on one
    # machine but not another depending on which libs are installed on both
    # machines.
    if 'make_command' in formula:
        module_dir = os.path.join(bru_modules_root, module_name, module_version)
        make_done_file = os.path.join(module_dir, "make_command.done")
        if not os.path.exists(make_done_file):
            make_commands = formula['make_command']
            system = platform.system()
            if not system in make_commands:
                raise Exception("no key {} in make_command".format(system))
            make_command = make_commands[system]
            with Chdir(module_dir):
                # todo: pick a make command depending on host OS
                print("building via '{}' ...".format(make_command))
                error_code = os.system(make_command)
                if error_code != 0:
                    raise ValueError("build failed with error code {}".format(error_code))
            touch(make_done_file)

def verify_resolved_dependencies(formula, target, resolved_dependencies):
    """ param formula is the formula with a bunch of desired(!) dependencies
        which after conflict resolution across the whole set of diverse deps
        may be required to pull a different version for that module for not
        violate the ODR. But which of course risks not compiling the module
        (but which hopefully will compile & pass tests anyway).
        Param resolved_dependencies is this global modulename-to-version map
        computed across the whole bru.json dependency list.
        Returns the subset of deps for the formula, using the resolved_dependencies
    """

    # this here's the module we want to resolve deps for now:
    module = formula['module']
    version = formula['version']

    # iterate over all target and their deps, fill in resolved versions
    target_name = target['target_name']
    resolved_target_deps = []

    def map_dependency(dep):
        """ param dep is a gyp file dependency, so either a local dep to a local
            target like 'zlib' or a cross-module dep like '../boost-regex/...'.
            There should be no other kinds of gyp deps in use """

        # detect the regexes as written by scan_deps.py: references into
        # a sibling module within ./bru_modules.
        bru_regex = "^../([^/]+)/([^/]+)\\.gyp:(.+)"
        match = re.match(bru_regex, dep)
        if match == None:
            return dep
        upstream_module = match.group(1)
        upstream_targets = match.group(2)
        if not upstream_module in resolved_dependencies:
            raise Exception("module {} listed in {}/{}.gyp's target '{}'"
                " not found. Add it to {}/{}.bru:dependencies"
                .format(
                    upstream_module, module, version, target_name,
                    module, version
                ))
        return resolved_dependencies[upstream_module]
    return list(map(map_dependency, target['dependencies']))

def compute_sources(formula, sources):
    """ gyp does not support glob expression or wildcards in 'sources', this
        here turns these glob expressions into a list of source files.
        param sources is target['sources'] or target['sources!']
    """
    def is_glob_expr(source):
        return '*' in source
    gyp_target_dir = os.path.join('bru_modules', formula['module']) # that is
        # the dir the gyp file for this module is being stored in, so paths
        # in the gyp file are interpreted relative to that
    result = []
    for source in sources:
        if source.startswith('ant:'):
            raise Exception('Ant-style glob exprs no longer supported: ' + source)
        if is_glob_expr(source):
            matching_sources = [os.path.relpath(filename, start=gyp_target_dir)
                                for filename in
                                glob.glob(os.path.join(gyp_target_dir, source))]
            assert len(matching_sources) > 0, "no matches for glob " + source
            result += matching_sources
        else:
            # source os a flat file name (relative to gyp parent dir)
            result.append(source)
    return list(sorted(result))

def copy_gyp(formula, resolved_dependencies):
    """
        Param resolved_dependencies is a superset of the deps in formula
        with recursively resolved module versions (after resolving conflicts).
    """

    # If the module has a gyp file then let's copy it into ./bru_modules/$module,
    # so next to the unpacked tar.gz, which is where the gyp file's relative
    # paths expect include_dirs and source files and such.
    # Not all modules need a gyp file, but a gyp file allows specifying upstream
    # module dependencies, whereas a ./configure; make might have easily overlooked
    # dependencies that result in harder-to-reproduce builds (unless you build
    # on only one single machine in your organization).
    # Actually even for modules build via make_command we need a gyp file to
    # specify include paths and module libs via all|direct_dependent_settings.
    #
    # Note that the gyp file in the ./library does not contain 'dependencies'
    # property yet, we add this property now (to not have the same redundant deps
    # both in *.bru and *.gyp in the ./library dir)
    module_name = formula['module']
    assert module_name in resolved_dependencies
    resolved_version = resolved_dependencies[module_name]
    gyp = get_library().load_gyp(formula)
    for target in gyp['targets']:

        if 'dependencies' in target:
            # Initially I thought there should be no such prop in the
            # library/.../*.gyp file because these deps will be filled in with
            # resolved deps from the *.bru file. But then I ran into two
            # problems:
            #   a) I wanted for example zlib tests to build via gyp also
            #      (espcially since zlib is being built via gyp target alrdy
            #      anyway), so the gyp test target should depend on the lib
            #      target.
            #   b) often test targets need to pull in additional module deps
            #      that the module (without its tests) does not depend on, for
            #      example tests often depend on googletest or googlemock,
            #      whereas the main module does not.
            # So now a *.bru file lists the union of dependencies for all
            # targets in a gyp file, while each target depends explicitly
            # lists dependencies as "bru:googletest". Could also support a
            # format like "bru:googletest:1.7.0" but then the *.gyp file
            # and *.bru file dependency lists would be redundant. Todo: move
            # dependency lists from *.bru to *.gyp file altogether? Maybe...
            verify_resolved_dependencies(formula, target, resolved_dependencies)

        # Sanity check: verify the 'sources' prop doesn't contain glob exprs
        # or wildcards: initially I though gyp was ok with
        #    "sources" : ".../src/*.cc"
        # in *.gyp files because at first glance this 'compiled', but it
        # turned out gyp just silently compiled zero source files in that case.
        #
        # Alternatively we could expand these wildcards now, drawback of that
        # is that the files in ./library are not really *.gyp files anymore,
        # and should probably be called *.gyp.in or *.gyp-bru or something
        # like that.
        for prop in ['sources', 'sources!']:
            if prop in target:
                target[prop] = compute_sources(formula, target[prop])

    # note that library/boost-regex/1.57.0.gyp is being copied to
    # bru_modules/boost-regex/boost-regex.gyp here (with some minor
    # transformations that were applied, e.g. expanding wildcards)
    gyp_target_file = os.path.join('bru_modules', module_name, module_name + ".gyp")

    # We also need a certain set of MSVC options imported into gyp files
    # and don't want to repeat the same boring MSVC settings in every single
    # module's individual gyp file. So add common.gypi include unless
    # the module's gyp file explicitly specifies includes already.
    if not 'includes' in gyp:
        # we want the 'includes' at the begin, to achieve this order see
        # http://stackoverflow.com/questions/16664874/how-can-i-add-the-element-at-the-top-of-ordereddict-in-python
        new_gyp = collections.OrderedDict()
        new_gyp['includes'] = ['../../bru_common.gypi']
        for key, value in gyp.items():
            new_gyp[key] = value
        gyp = new_gyp

    brulib.jsonc.savefile(gyp_target_file, gyp)

    # this file is only saved for human reader's sake atm:
    brulib.jsonc.savefile(os.path.join('bru_modules', module_name, 'bru-version.json'),
        {'version': resolved_version})

def resolve_conflicts(dependencies):
    """ takes a dict of modules and version matchers and recursively finds
        all indirect deps. Then resolves version conflicts by picking the newer
        of competing deps, or by picking the version that was requested by the module
        closest to the root of the dependency tree (unsure still).
    """
    root_requestor = 'bru.json'
    todo = [(module, version, root_requestor) for (module, version)
            in dependencies.items()]
    recursive_deps = collections.OrderedDict()
    for module_name, version_matcher, requestor in todo:
        module_version = version_matcher # todo: allow for npm-style version specs (e.g. '4.*')
        #print('resolving dependency {} version {} requested by {}'
        #      .format(module_name, module_version, requestor))
        if module_name in recursive_deps:
            resolved = recursive_deps[module_name]
            resolved_version = resolved['version']
            if module_version != resolved_version:
                winning_requestor = resolved['requestor']
                print("WARNING: version conflict for {} requested by first {} and then {}"
                      .format(module_name, winning_requestor, requestor))
                # instead of just letting the 2nd and later requestors loose
                # the competition we could probably do something more sensible.
                # todo?
        else:
            # this is the first time this module was requested, freeze that
            # chosen version:
            formula = get_library().load_formula(module_name, module_version)
            recursive_deps[module_name] = {
                'version' : module_version,
                'requestor' : requestor
            }

            # then descend deeper into the dependency tree:
            deps = formula['dependencies'] if  'dependencies' in formula else {}
            child_requestor = module_name
            todo += [(child_module, version, child_requestor)
                     for (child_module, version)
                     in deps.items()]

    return [(module, resolved['version'], resolved['requestor'])
            for (module, resolved) in recursive_deps.items()]

def get_single_bru_file(dir):
    """ return None of no *.bru file in this dir """
    matches = glob.glob("*.bru")
    if len(matches) == 0:
        return None
    if len(matches) > 1:
        raise Exception("there are multiple *.bru files in {}: {}".format(
              dir, matches))
    return os.path.join(dir, matches[0])

def get_or_create_single_bru_file(dir):
    """ returns single *.bru file from given dir or creates an empty
        package.bru file (corresponding to package.json for npm).
        So unlike get_single_bru_file() never returns None.
    """
    bru_file = get_single_bru_file(dir)
    if bru_file == None:
        bru_file = os.path.join(dir, 'package.bru')
        brulib.jsonc.savefile(bru_file, {'dependencies':{}})
        print('created ', bru_file)
    assert bru_file != None
    return bru_file

def parse_module_at_version(installable):
    """ parses 'googlemock@1.7.0' into tuple (module, version),
        and returns (module, None) for input lacking the @version suffix.
    """
    elems = installable.split('@')
    if len(elems) == 1:
        return Installable(elems[0], None)
    if len(elems) == 2:
        return Installable(elems[0], elems[1])
    raise Exception("expected module@version but got {}".format(installable))

def parse_existing_module_at_version(installable):
    """ like parse_module_at_version but returns the latest version if version
        was unspecified. Also verifies module at version actually exist
        in ./library.
    """
    installable = parse_module_at_version(installable)
    module = installable.module
    version = installable.version
    library = get_library()
    if not os.path.exists(library.get_module_dir(module)):
        raise Exception("no module {} in {}, may want to 'git pull'"\
              " if this module was added very recently".format(
              module, library.get_root_dir()))
    if version == None:
        version = library.get_latest_version_of(module)
    if not library.has_formula(module, version):
        raise Exception("no version {} in {}/{}, may want to 'git pull'"\
              " if this version was added very recently".format(
              version, library.get_root_dir(), module))
    assert version != None
    return Installable(module, version)

def add_dependencies_to_bru(bru_filename, installables):
    bru = brulib.jsonc.loadfile(bru_filename)
    if not 'dependencies' in bru:
        bru['dependencies'] = {}
    deps = bru['dependencies']
    for installable in installables:
        deps[installable.module] = installable.version
    brulib.jsonc.savefile(bru_filename, bru) # warning: this nukes comments atm

def add_dependencies_to_gyp(gyp_filename, installables):
    gyp = brulib.jsonc.loadfile(gyp_filename)
    # typically a gyp file has multiple targets, e.g. a static_library and
    # one or more test executables. Here we add the new dep to only the first
    # target in the gyp file, which is somewhat arbitrary. TODO: revise.
    # Until then end user can always shuffle around dependencies as needed
    # between targets.
    if not 'targets' in gyp:
        gyp['targets'] = []
    targets = gyp['targets']
    if len(targets) == 0:
        targets[0] = {}
    first_target = targets[0]
    if not 'dependencies' in first_target:
        first_target['dependencies'] = []
    deps = first_target['dependencies']
    for installable in installables:
        module = installable.module
        dep_gyp_path = "bru_modules/{}/{}.gyp".format(module, module)
        dep_expr = dep_gyp_path + ":*" # depend on all targets, incl tests
        if not dep_expr in deps:
            deps.append(dep_expr)
    brulib.jsonc.savefile(gyp_filename, gyp) # warning: this nukes comments atm

def create_gyp_file(gyp_filename):
    """ creates enough of a gyp file so that we can record dependencies """
    if os.path.exists(gyp_filename):
        raise Exception('{} already exists'.format(gyp_filename))
    gyp = collections.OrderedDict([
        ("includes", ["bru_common.gypi"]),
        ("targets", [
            collections.OrderedDict([
                ("target_name", "foo"), # just a guess, user should rename
                ("type", "none"), # more likely 'static_library' or 'executable'

                # these two props are going to have to be filled in by enduser
                ("sources", []),
                ("includes_dirs", []),

                ("dependencies", [])
        ])])
    ])
    brulib.jsonc.savefile(gyp_filename, gyp)

class Installable:
    def __init__(self, module, version):
        self.module = module
        self.version = version

def cmd_install(installables):
    """ param installables: e.g. [] or ['googlemock@1.7.0', 'boost-regex']
        This is supposed to mimic 'npm install' syntax, see
        https://docs.npmjs.com/cli/install. Examples:
          a) bru install googlemock@1.7.0
          b) bru install googlemock
          c) bru install
        Variant (a) is self-explanatory, installing the module of the given
        version. Variant (b) installs the latest known version of the module
        as specified by the versions listed in bru/library/googlemock.
        Variant (c) will install all dependencies listed in the local *.bru
        file (similar as how 'npm install' install all deps from ./package.json).
        Unlike for 'npm install' the option --save is implied, means whatever you
        install will end up in the local *.bru file's "dependencies" list, as
        well as in the companion *.gyp file.
    """
    if len(installables) == 0:
        # 'bru install'
        bru_filename = get_single_bru_file(os.getcwd())
        if bru_filename == None:
            raise Exception("no file *.bru in cwd")
        print('installing dependencies listed in', bru_filename)
        install_from_bru_file(bru_filename)
    else:
        # installables are ['googlemock', 'googlemock@1.7.0']
        installables = [parse_existing_module_at_version(installable)
                        for installable in installables]
        bru_filename = get_or_create_single_bru_file(os.getcwd())
        gyp_filename = bru_filename[:-3] + 'gyp'
        if not os.path.exists(gyp_filename):
            create_gyp_file(gyp_filename)
        add_dependencies_to_bru(bru_filename, installables)
        add_dependencies_to_gyp(gyp_filename, installables)
        for installable in installables:
            print("added dependency {}@{} to {} and {}".format(
                installable.module, installable.version,
                bru_filename, gyp_filename))
        # now download the new dependency just like 'bru install' would do
        # after we added the dep to the bru & gyp file:
        install_from_bru_file(bru_filename)

def install_from_bru_file(bru_filename):
    package_jso = brulib.jsonc.loadfile(bru_filename)
    recursive_deps = resolve_conflicts(package_jso['dependencies'])
    resolved_dependencies = dict((module, version)
        for (module, version, requestor) in recursive_deps)
    for module_name, module_version, requestor in recursive_deps:
        print('processing dependency {} version {} requested by {}'
              .format(module_name, module_version, requestor))
        formula = get_library().load_formula(module_name, module_version)
        get_dependency(module_name, module_version)
        copy_gyp(formula, resolved_dependencies)

    # copy common.gypi which is referenced by module.gyp files and usually
    # also by the parent *.gyp (e.g. bru-sample:foo.gyp).
    # Should end users be allowed to make changes to bru_common.gypi or
    # should they rather edit their own optional common.gpyi which shadows
    # bru_common.gypi? Unsure, let them edit for now, so dont overwrite gypi.
    common_gypi = 'bru_common.gypi'
    if not os.path.exists(common_gypi):
        print('copying', common_gypi)
        shutil.copyfile(
            os.path.join(get_script_path(), common_gypi),
            common_gypi)

    #for module, version, requestor in recursive_deps:
    #    for ext in ['bru', 'gyp']:
    #        print("git add -f library/{}/{}.{}".format(module, version, ext))


    # todo: clean up unused module dependencies from /bru_modules?

def get_test_targets(gyp):
    """ returns the subset of gyp targets that are tests """

    # Each module typically declares a static lib (usually one, sometimes
    # several), as well as one or more tests, and in rare cases additional
    # executables, e.g. as utilities.
    # How can we tell which of the targets are tests? Heuristically static libs
    # cannot be tests, executables usually but not always are. What many tests
    # still need though is a cwd at startup (e.g. to find test data), which
    # differs from module to module. Let's add this test/cwd property to the
    # gyp target, gyp will ignore such additional props silently. This test/cwd
    # is interpreted relative to the gyp file (like any other path in a gyp file).
    targets = gyp['targets']
    for target in targets:
        if 'test' in target:
            yield target

class TestResult(Enum):
    fail = 0
    success = 1
    notrun = 2 # e.g. not run because executable wasnt built or wasnt found

class CompletedTestRun:
    def __init__(self, target_name, test_result):
        """ param test is a gyp target name for given module, e.g. 'googlemock_test'
            param result is a test result
            param result is of type TestResult
        """
        self.target_name = target_name
        self.test_result = test_result
        self.module = None
        self.duration_in_ms = -1 # -1 means test wasnt run yet

def locate_executable(target_name):
    """ return None if it (likely) wasnt built yet (or if for some other reason
        we cannot determine where the executable was put by whatever toolchain
        gyp was triggering.
        Otherwise return relative path to executable.
    """
    for config in ['Release', 'Debug']:
        candidates = [
            os.path.join('out', config, target_name),
            os.path.join('out', config, target_name + '.exe'),
            os.path.join(config, target_name),
            os.path.join(config, target_name + '.exe')]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
    return None

def exec_test(gypdir, target):
    """ runs test (if executable was built) and returns an instance of
        CompletedTestRun.
        param gypdir is the location of the gyp file
        param target is a 'target' node from a gyp file, so a dict with
              keys like gyp's 'target_name' and the bru-specific 'test'
    """

    # Now knowing the target we have the following problems:
    # * where is the compiled target executable located? E.g. on Ubuntu
    #   with make I find it in out/Release/googlemock_test but on Windows
    #   with msvs it ends in Release/googlemock_test.exe
    # * run Debug or Release or some other config? Let's run Release only
    #   for now. TODO: make configurable, or use Release-Debug fallback?
    #   Or run whichever was built last?
    target_name = target['target_name']
    exe_path = locate_executable(target_name)
    if exe_path != None:
        print('running', target_name)
        t0 = time.time() # or clock()?
        test = target['test']
        rel_cwd = test['cwd'] if 'cwd' in test else './'
        test_argv = test['args'] if 'args' in test else []
        test_stdin = test['stdin'] if 'stdin' in test else None
        proc = subprocess.Popen([os.path.abspath(exe_path)] + test_argv,
                                cwd = os.path.join(gypdir, rel_cwd),
                                stdin = subprocess.PIPE if test_stdin != None else None)
        if test_stdin != None:
            # from http://stackoverflow.com/questions/163542/python-how-do-i-pass-a-string-into-subprocess-popen-using-the-stdin-argument
            proc.stdin.write(test_stdin.encode('utf8'))
            proc.stdin.close() # signal eos ?
            proc.communicate() # necessary ?
        proc.wait()
        returncode = proc.returncode
        duration_in_ms = int(1000 * (time.time() - t0))
        print(target_name, 'returned with exit code', returncode, 'after',
            duration_in_ms, 'ms')
        testrun = CompletedTestRun(target_name,
            TestResult.success if returncode == 0 else TestResult.fail)
        testrun.duration_in_ms = duration_in_ms
        return testrun
    else:
        print('cannot find executable', target_name)
        return CompletedTestRun(target_name, TestResult.notrun)

def collect_tests(module_names):
    """ yields tuples (module, gypdir, test_target), where gypdir is the
        directory the *gyp file is located in (since all file paths in the
        gyp - e.g. the test.cwd - are relative to that particular dir """
    modules_dir = 'bru_modules'
    for module in module_names:
        gypdir = os.path.join(modules_dir, module)
        gyp = brulib.jsonc.loadfile(os.path.join(
                gypdir, module + ".gyp"))
        test_targets = get_test_targets(gyp)
        for test_target in test_targets:
            yield (module, gypdir, test_target)

def cmd_test(testables):
    """ param testables is list of module names, empty to test all modules
    """

    # You alrdy can run tests for upstream deps via these cmds, here
    # for example for running zlib tests:
    #   >bru install
    #   >cd bru_modules/zlib
    #   >gyp *.gyp --depth=.
    #   >make
    #   >out/Default/zlib_test
    # This command here is supposed to automate these steps: you can run these
    # commands here:
    #   >bru test  # runs tests for all modules in ./bru_modules
    #   >bru test boost-regex  # runs tests for given modules only

    modules_dir = 'bru_modules'
    if len(testables) == 0:
        for gyp_filename in glob.glob(os.path.join(modules_dir, '**', '*.gyp')):
            module_dir = os.path.dirname(gyp_filename)
            _, module = os.path.split(module_dir)
            testables.append(module)

    # first let's check if all tests were built already, if they weren't then
    # we'll do an implicit 'bru make' before running tests
    def did_all_builds_complete(testables):
        for module, gypdir, target in collect_tests(testables):
            target_name = target['target_name']
            if locate_executable(target_name) == None:
                print("executable for {}:{} not found".format(
                    module, target_name))
                return False
        return True
    if not did_all_builds_complete(testables):
        print("running 'bru make':")
        cmd_make()

    testruns = []
    module2test_count = dict((module, 0) for module in testables)
    for module, gypdir, target in collect_tests(testables):
        testrun = exec_test(gypdir, target)
        testrun.module = module
        testruns.append(testrun)
        module2test_count[module] += 1
        
    # for modules that don't have tests defined yet strive for adding
    # some tests, warn/inform the user about these modules here:
    modules_without_tests = [module 
                for (module, test_count) in module2test_count.items()
                if test_count == 0]

    # also partition/group testruns by test result:                
    testgroups = {} # testresult to list of testruns, so partitioned testruns
    for test_result, runs in itertools.groupby(testruns,
                             lambda testrun: testrun.test_result):
        testgroups[test_result] = list(runs)

    def get_test_group(test_result):
        return testgroups[test_result] if test_result in testgroups else []

    def print_test_group(msg, test_result):
        testgroup = list(get_test_group(test_result))
        if len(testgroup) > 0:
            print(msg.format(len(testgroup)))
            for testrun in testgroup:
                line = '  {}:{}'.format(testrun.module, testrun.target_name)
                if testrun.duration_in_ms >= 0:
                    line += ' after {} ms'.format(testrun.duration_in_ms)
                print(line)
        return len(testgroup)

    print("test summary:")
    if len(modules_without_tests) > 0:
        print('warning: the following modules have no tests configured:')
        for module in sorted(modules_without_tests):
            print('  ', module)
    successful_test_count = print_test_group(
        'The following {} tests succeeded:', 
        TestResult.success)
    missing_test_count = print_test_group(
        'The following {} tests are missing (build failed?):', 
        TestResult.notrun)
    failed_test_count = print_test_group(
        'The following {} tests failed:', 
        TestResult.fail)
    if missing_test_count > 0 or failed_test_count > 0:
        raise Exception('ERROR: {} tests failed and {} tests failed building'\
                        .format(failed_test_count, missing_test_count))
    print('All {} tests successful.'.format(successful_test_count))

def cmd_make():
    """ this command makes some educated guesses about which toolchain
        the user probably wants to run, then invokes gyp to create the
        makefiles for this toolchain and invokes the build. On Linux
        'bru make' is likely equivalent to:
           >gyp *gyp --depth=.
           >make
        On Windows it's likely equivalent to:
           >gyp --depth=. package.gyp -G msvs_version=2012
           >C:\Windows\Microsoft.NET\Framework\v4.0.30319\msbuild.exe package.sln
        The main purpose of 'bru make' is really to spit out these two
        command lines as a quick reminder for how to build via cmd line.
    """

    # first locate the single gyp in the cwd
    bru_file = get_single_bru_file('.')
    if bru_file == None:
        raise Exception("there's no *.bru file in current work dir, "
            'e.g. run "bru install googlemock" first to create one')
    gyp_file = bru_file[:-3] + 'gyp'
    if not os.path.exists(gyp_file):
        raise Exception(bru_file,'has no companion *.gyp file, '
            'e.g. recreate one via "bru install googlemock"')

    system = platform.system()
    if system == 'Windows':
        cmd_make_win(gyp_file)
    elif system == 'Linux':
        cmd_make_linux(gyp_file)
    else:
        raise Exception('no idea how to invoke gyp & toolchain on platform {}'\
            .format(system))

def get_latest_msbuild_exe():
    """ return path to latest msbuild on Windows machine """
    env = os.environ
    windir = env['SystemRoot'] if 'SystemRoot' in env else env['windir']
    glob_expr = os.path.join(windir, 'Microsoft.NET', 'Framework',
        '**', 'msbuild.exe')
    msbuilds = glob.glob(glob_expr)
    return max(msbuilds)  # not alphanumeric, should be good enough tho

def get_latest_msvs_version():
    """ e.g. return 2012 (aka VC11) if msvs 2012 is installed. If multiple
        vs versions are installed then pick latest.
        Return None if no installs are found?
    """
    # whats a good way to detect the msvs version?
    # a) scan for install dirs like
    #    c:\Program Files (x86)\Microsoft Visual Studio 10.0
    # b) scan for env vars like VS110COMNTOOLS
    # Let's do (b) for now.
    # See also https://code.google.com/p/gyp/source/browse/trunk/pylib/gyp/MSVSVersion.py
    msvs_versions = []
    regex = re.compile('^VS([0-10]+)COMNTOOLS$')
    for key in os.environ:
        match = regex.match(key)
        if match != None:
            msvs_versions.append(int(match.group(1)))
    if len(msvs_versions) == 0:
        return None
    latest = max(msvs_versions) # e.g. 110
    if len(msvs_versions) > 1:
        print('detected installs of msvs {}, choosing latest {}'.format(
            msvs_versions, latest))
    msvs_version2year = {
        80: 2005,
        90: 2008,
        100: 2010,
        110: 2012,
    }
    if not latest in msvs_version2year:
        print('not sure how to map VC{} to a VS year, defaulting to VS 2012'
            .format(latest))
        return 2012
    return msvs_version2year[latest]

def run_gyp(gyp_cmdline):
    print('running >', gyp_cmdline)
    returncode = os.system(gyp_cmdline)
    if returncode != 0:
        raise Exception('error running gyp, did you install it?'
            ' Instructions at https://github.com/KjellSchubert/bru')

def cmd_make_win(gyp_filename):
    # TODO: locate msvs version via glob
    msvs_version = get_latest_msvs_version()
    if msvs_version == None:
        print('WARNING: no msvs installation detected, did you install it? '
            'Defaulting to msvs 2012.')
    gyp_cmdline = 'gyp --depth=. {} -G msvs_version={}'.format(
        gyp_filename, msvs_version)
    run_gyp(gyp_cmdline)
    # gyp should have created a *.sln file, verify that.
    # if it didnt that pass a msvc generator option to gyp in a more explicit
    # fashion (is -G msvs_version enough? need GYP_GENERATORS=msvs?).
    sln_filename = gyp_filename[:-3] + 'sln'
    if not os.path.exists(sln_filename):
        raise Exception('gyp unexpectedly did not generate a *.sln file, '
            'you may wanna invoke gyp manually to generate the expected '
            'make/sln/ninja files, e.g. set GYP_GENERATORS=msvs')

    # there are many ways to build the *.sln now, e.g. pass it to devenv
    # or alternatively to msbuild. Lets do msbuild here:
    # TODO locate msbuild via glob
    msbuild_exe = get_latest_msbuild_exe()
    if msbuild_exe == None:
        raise Exception('did not detect any installs of msbuild, these should'
            ' be part of .NET installations, please install msbuild or .NET')
    config = 'Release'
    msbuild_cmdline = '{} {} /p:Configuration={}'.format(
        msbuild_exe, sln_filename, config)
    print('running msvs via msbuild >', msbuild_cmdline)
    returncode = os.system(msbuild_cmdline)
    if returncode != 0:
        raise Exception('msbuild failed with errors, returncode =', returncode)
    print('Build complete.')

def cmd_make_linux(gyp_filename):
    # Here we could check if ninja or some such is installed to generate ninja
    # project files. But for simplicity's sake let's just use whatever gyp
    # defaults to.

    # For some odd reason passing './package.gyp' as a param to gyp will 
    # generate garbage, instead you gotta pass 'package.gyp'. Se let's 
    # explicitly remove a leading ./
    dirname = os.path.dirname(gyp_filename)
    assert dirname == '.' or len(dirname) == 0
    gyp_filename = os.path.basename(gyp_filename)
    
    gyp_cmdline = 'gyp --depth=. {}'.format(gyp_filename)
    run_gyp(gyp_cmdline)
    if not os.path.exists('Makefile'):
        raise Exception('gyp did not generate ./Makefile, no idea how to '
            'build with your toolchain, please build manually')
    returncode = os.system('make')
    if returncode != 0:
        raise Exception('Build failed: make returned', returncode)
    print('Build complete.')

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    parser_install = subparsers.add_parser('install')
    parser_install.add_argument("installables", default = [], nargs = '*',
                                help = 'e.g. googlemock@1.7.0')

    parser_test = subparsers.add_parser('test')
    parser_test.add_argument("testables", default = [], nargs = '*',
                                help = 'e.g. googlemock')

    parser_test = subparsers.add_parser('make')

    args = parser.parse_args()
    if args.command == 'install':
        cmd_install(args.installables)
    elif args.command == 'make':
        cmd_make()
    elif args.command == 'test':
        cmd_test(args.testables)
    else:
        raise Exception("unknown command {}, chose install | test".format(args.command))

if __name__ == "__main__":
    main()
