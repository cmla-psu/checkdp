import argparse
import sys
import time
import logging
import shutil
import math
import tempfile
import re
import pathlib
import coloredlogs
import checkdp.core as core
import checkdp.transform.constants as constants
from checkdp.symex import KLEE, Z3
from checkdp.clang import Clang
from checkdp.transform import transform
from checkdp.validate import PSI

coloredlogs.install('DEBUG', fmt='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def main(argv=tuple(sys.argv[1:])):
    current_folder = pathlib.Path.cwd()
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument('file', metavar='FILE', type=str, nargs=1)
    arg_parser.add_argument('-k', '--klee',
                            action='store', dest='klee', type=str,
                            default=current_folder / 'klee' / 'build' / 'bin' / 'klee',
                            help='The klee binary path', required=False)
    arg_parser.add_argument('--kleaver',
                            action='store', dest='kleaver', type=str,
                            default=current_folder / 'klee' / 'build' / 'bin' / 'kleaver',
                            help='The kleaver binary path', required=False)
    arg_parser.add_argument('--z3',
                            action='store', dest='z3', type=str,
                            default=current_folder / 'z3' / 'bin' / 'z3',
                            help='The z3 binary path', required=False)
    arg_parser.add_argument('-i', '--include',
                            action='store', dest='include', type=str,
                            default=current_folder / 'klee' / 'include',
                            help='The include path for klee', required=False)
    arg_parser.add_argument('-c', '--clang',
                            action='store', dest='clang', type=str,
                            default=current_folder / 'klee' / 'deps' / 'llvm-9.0' / 'bin' / 'clang',
                            help='The clang binary path', required=False)
    arg_parser.add_argument('-o', '--out',
                            action='store', dest='out', type=str,
                            default=current_folder / 'checkdp-out',
                            help='The output path for CheckDP', required=False)
    arg_parser.add_argument('-l', '--loglevel',
                            action='store', dest='loglevel', type=str, default='debug',
                            help='The log level for the logger, could be one of {debug, info, warning, error}',
                            required=False)
    arg_parser.add_argument('-p', '--psi',
                            action='store', dest='psi', default=current_folder / 'psi' / 'psi',
                            help='The path for psi binary',
                            required=False)
    arg_parser.add_argument('-s', '--psisource',
                            action='store', dest='psi_source', default=None,
                            help='The source file for psi',
                            required=False)
    arg_parser.add_argument('--search-heuristic',
                            action='store', dest='search', default='dfs',
                            help='The search heuristic for KLEE, see klee --help for available options.',
                            required=False)
    arg_parser.add_argument('--transform-only', dest='transform_only', action='store_true',
                            default=False, required=False,
                            help='Only generate the transformed template')
    arg_parser.add_argument('--enable-shadow', dest='enable_shadow', action='store_true', default=False, required=False,
                            help='Controls whether shadow execution is used or not.')

    arguments = arg_parser.parse_args(argv)

    # setup the logger
    levels = {'debug': logging.DEBUG, 'info': logging.INFO, 'warning': logging.WARNING, 'error': logging.ERROR}
    try:
        level = levels[arguments.loglevel.lower()]
        logging.getLogger('checkdp').setLevel(level)
        logger.info(f'Log level set to {logging.getLevelName(level)}')
    except KeyError:
        logger.error('log level should be one of {debug, info, warning, error}')
        return 1

    # cleanup the output folder
    output_folder = pathlib.Path(arguments.out)
    if output_folder.exists():
        shutil.rmtree(output_folder)
    output_folder.mkdir()

    # add a file handler to logging module for persistent storage
    file_handler = logging.FileHandler(output_folder / 'run.log', mode='w')
    file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger('checkdp').addHandler(file_handler)

    start = time.time()
    # create a temporary directory for klee output
    with tempfile.TemporaryDirectory(prefix='klee') as temp_dir:
        try:
            clang = Clang(arguments.clang, [arguments.include, pathlib.Path.cwd()])
            # use Clang to first do syntax check
            # CAVEAT: the following code does not work on windows, since fp cannot be opened again on windows NT, see
            # https://docs.python.org/3.8/library/tempfile.html#tempfile.NamedTemporaryFile
            with tempfile.NamedTemporaryFile('w+') as fp:
                with open(arguments.file[0], 'r') as original_fp:
                    source = original_fp.read()
                fp.write(re.sub(f'{constants.HOLE}_\\d+', '1', source))
                fp.flush()
                clang.syntax_check(fp.name)

            # preprocess the source file (remove the comments etc that cannot be parsed by plyparser)
            preprocessed_file = output_folder / 'preprocessed.c'
            clang.preprocess(arguments.file[0], preprocessed_file)

            # transform the code to a template
            with preprocessed_file.open('r') as file:
                code = file.read()
            template = transform(code, arguments.enable_shadow)

            with open(output_folder / 'template.c', 'w') as template_file:
                template_file.write(template.fill_default(5))

            if arguments.transform_only:
                return 0
            klee_out = pathlib.Path.cwd() / 'klee-out'
            logger.info(f'KLEE output dir {klee_out}, KLEE search heuristic: {arguments.search}')
            z3_obj = Z3(arguments.z3, klee_out)
            klee = KLEE(arguments.klee, arguments.kleaver, z3_obj, klee_out, search_heuristic=arguments.search)

            is_alignment, *rest = core.run(template, klee, clang, output_folder)
            if is_alignment:
                alignment = rest[0]
                logger.info(f'Result: Alignment Found: {template.random_distance(alignment)}')
                if arguments.enable_shadow:
                    logger.info(f'Selector: {template.selector(alignment)}')
                return 0
            else:
                counterexample, bad_outputs = rest
            logger.info(f'Result: Counterexample Found: {counterexample} with output {bad_outputs}')
            logger.info(f'Total time for running checkdp: {time.time() - start} s')
            # split the counterexample (with q and dq variables) into two separate inputs
            if arguments.psi_source and arguments.psi:
                final_bad_output, final_pa, final_pb, final_ratio = None, None, None, None
                logger.info('PSI validation source file detected, running validation')
                epsilon = 1
                psi_solver = PSI(arguments.psi, output_folder)

                for output in bad_outputs:
                    pa, pb = psi_solver.validate(arguments.psi_source,
                                                 counterexample, template.related_inputs(counterexample),
                                                 output)
                    logger.debug(f'pa: {pa}, pb: {pb}')
                    try:
                        if pa == pb == 0:
                            ratio = 0
                        else:
                            ratio = pa / pb if pa > pb else pb / pa
                    except ZeroDivisionError:
                        ratio = float('inf')
                    # TODO: quick hack, fix later
                    check_ratio = 2 * epsilon if 'smartsum' in arguments.file[0] else epsilon
                    if ratio > math.pow(math.e, check_ratio):
                        final_bad_output, final_pa, final_pb, final_ratio = output, pa, pb, ratio
                        break
                # if any of the bad output validates
                if final_bad_output:
                    logger.info(f'PSI validation passed, final bad output {final_bad_output}')
                    logger.info(f'Validated counterexample {counterexample}')
                    logger.info(f'bad output is {final_bad_output}')
                    logger.info(f'Pa = {final_pa}, Pb = {final_pb}, Ratio = {ratio} > e^({epsilon})')
                    logger.info('Result: Counterexample Found')
                else:
                    logger.error('PSI validation failed, ratio of probabilities is still bounded')
                    return 1
        finally:
            logger.info(f'Finished in {time.time() - start} seconds.')
            file_handler.flush()
            logging.getLogger('checkdp').removeHandler(file_handler)


if __name__ == '__main__':
    main()
