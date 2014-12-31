#!/usr/bin/env python3

import os
import argparse
import pdb # only if you want to add pdb.set_trace()
import brulib.jsonc
import brulib.library
import brulib.install
import brulib.make
import brulib.runtests

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

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    parser_install = subparsers.add_parser('install')
    parser_install.add_argument("installables", default = [], nargs = '*',
                                help = 'e.g. googlemock@1.7.0')

    parser_test = subparsers.add_parser('test')
    parser_test.add_argument("testables", default = [], nargs = '*',
                                help = 'e.g. googlemock')

    parser_make = subparsers.add_parser('make')
    parser_make.add_argument('--config', default='Release')

    args = parser.parse_args()
    library = get_library()
    if args.command == 'install':
        brulib.install.cmd_install(library, args.installables)
    elif args.command == 'make':
        brulib.make.cmd_make(args.config)
    elif args.command == 'test':
        brulib.runtests.cmd_test(args.testables)
    else:
        raise Exception("unknown command {}, chose install | test".format(args.command))

if __name__ == "__main__":
    main()
