#!/usr/bin/env python3
import time
_START = time.perf_counter()

from gnmi_agent.go_gnmi_utils import (  # noqa: E402
    apply_gnmi_data, gnmi_get, gnmi_set, GNMIEnvironment,
    TIMINGS, phase, dump_timings,
)
import argparse  # noqa: E402
from jinja2 import Environment, FileSystemLoader  # noqa: E402
import orjson  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sys  # noqa: E402
import logging  # noqa: E402

TIMINGS["module_imports"] = [1, time.perf_counter() - _START]

SUPPORTED_EXTS = ('.j2', '.json', '.jsonc')

# String-aware C-comment stripper for .jsonc:
#   - quoted strings are matched and preserved verbatim
#   - // line comments and /* block comments */ are matched and dropped
_JSONC_TOKEN_RE = re.compile(
    r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"',
    re.DOTALL,
)


def _strip_jsonc_comments(text):
    def repl(m):
        token = m.group(0)
        return '' if token.startswith('/') else token
    return _JSONC_TOKEN_RE.sub(repl, text)


def load_template(template_path, context, reverse=False):
    """Load and parse a config template, returning a Python list of ops.

    For .j2 the template is rendered then JSON-parsed; for .json/.jsonc the
    file is read (and comments stripped for .jsonc) then JSON-parsed. The
    caller hands the parsed list straight to apply_gnmi_data -- no temp
    file, no second read.
    """
    ext = os.path.splitext(template_path)[1].lower()
    if ext not in SUPPORTED_EXTS:
        print("error: unsupported file extension '%s'; supported: %s"
              % (ext, ', '.join(SUPPORTED_EXTS)))
        sys.exit(2)

    if ext == '.j2':
        with phase("jinja_render"):
            search_dir = os.path.dirname(os.path.abspath(template_path)) or '.'
            template_name = os.path.basename(template_path)
            env = Environment(loader=FileSystemLoader(search_dir))
            template = env.get_template(template_name)
            rendered_content = template.render(context)
        with phase("json_load"):
            res = orjson.loads(rendered_content)
    elif ext == '.json':
        with phase("json_load"):
            with open(template_path, 'rb') as f:
                res = orjson.loads(f.read())
    else:  # .jsonc
        with phase("jsonc_load"):
            with open(template_path, 'r') as f:
                res = orjson.loads(_strip_jsonc_comments(f.read()))

    if reverse and isinstance(res, list):
        res = res[::-1]
    return res


# overide error method, to display help message on error as well
class MyParser(argparse.ArgumentParser):
    def error(self, message):
        print('error: %s\n' % message)
        self.print_help()
        raise argparse.ArgumentTypeError(message)


def int_range_type(min_val, max_val):
    def check_range(value):
        ivalue = int(value)
        if ivalue < min_val or ivalue > max_val:
            raise argparse.ArgumentTypeError(f"Value must be between {min_val} and {max_val}")
        return ivalue
    return check_range


# parse command line argments and return result
def parse_args():
    # Create the parser
    parser = MyParser(description='Parse command line arguments')
    parser.add_argument('-t', '--target', type=str, default="127.0.0.1:8080",
                        help='GNMI server address in the format of host:port')
    parser.add_argument('-l', '--log-level',
                        choices=['debug', 'info', 'warning', 'error'],
                        default='warning',
                        help='logging level (default: warning -- quiet)')
    parser.add_argument('-i', "--dpu_index", type=int_range_type(0, 7), default=0, required=False,
                        help="DPU index [0-7]")
    parser.add_argument('-n', "--num_dpus", type=int_range_type(1, 8), default=1, required=False, help="Number of DPUs")
    parser.add_argument('-s', "--sleep_secs", type=int, default=0, required=False,
                        help="Delay before each batch operation in seconds")
    parser.add_argument('-b', "--batch_val", type=int, default=10, required=False, help="Batch operation size")
    parser.add_argument('-u', '--username', type=str, default="admin", help='GNMI server user name')
    parser.add_argument('-p', '--password', type=str, default="password", help='GNMI server password')

    # Create the subparser
    subparsers = parser.add_subparsers(title='subcommands', dest='topsubcmd', required=True)
    update_parser = subparsers.add_parser('update', help='Update operation')
    update_parser.add_argument('-f', '--filename', type=str, required=True, help='the path of json template file')

    replace_parser = subparsers.add_parser('replace', help='Replace operation')
    replace_parser.add_argument('-f', '--filename', type=str, required=True, help='the path of json template file')

    delete_parser = subparsers.add_parser('delete', help='Delete operation')
    delete_group = delete_parser.add_mutually_exclusive_group(required=True)
    delete_group.add_argument('-f', '--filename', type=str, help='the path of json template file')
    delete_group.add_argument('-x', '--xpath', type=str, help='the xpath of the object to be deleted')

    get_parser = subparsers.add_parser('get', help='Get operation')
    get_group = get_parser.add_mutually_exclusive_group(required=True)
    get_group.add_argument('-f', '--filename', type=str, help='the path of json template file')
    get_group.add_argument('-x', '--xpath', type=str, help='the xpath of the object to return')

    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format='%(asctime)s - %(levelname)s - %(message)s')
    return args


def exec_action(args):
    env = GNMIEnvironment()
    env.username = args.username
    env.password = args.password
    target = args.target.split(":", 1)
    env.gnmi_ip = target[0]
    if len(target) == 1:
        env.gnmi_port = 8080
    else:
        env.gnmi_port = int(target[1])
    env.dpu_index = args.dpu_index
    env.num_dpus = args.num_dpus
    template_args = {}
    template_args['dpu_index'] = env.dpu_index
    template_args['num_dpus'] = env.num_dpus

    if not args.filename:
        if args.topsubcmd == "delete":
            gnmi_set(env, [args.xpath], [], [])
        elif args.topsubcmd == "get":
            gnmi_get(env, [args.xpath])
        return
    reverse = False
    if args.topsubcmd == "update":
        template_args['op'] = "SET"
    elif args.topsubcmd == "replace":
        template_args['op'] = "REP"
    elif args.topsubcmd == "delete":
        template_args['op'] = "DEL"
        reverse = True
    else:
        template_args['op'] = "GET"
    res = load_template(args.filename, template_args, reverse)
    apply_gnmi_data(env, res, args.batch_val, args.sleep_secs)


def main():
    try:
        parsedArgs = parse_args()
    except argparse.ArgumentTypeError as e:
        # Handle the error
        print(str(e))
        return
    if not parsedArgs:
        return
    exec_action(parsedArgs)


if __name__ == '__main__':
    main()
    dump_timings()
    print("elapsed: %.3fs" % (time.perf_counter() - _START))
