import logging
import subprocess
import sys


logger = logging.getLogger(__name__)


class Clang:
    def __init__(self, binary, include_dirs=(), library_dirs=(), extra_args=()):
        self._binary = binary
        self._include_dirs = list(include_dirs)
        self._library_dirs = list(library_dirs)
        self._extra_args = list(extra_args)
        # under macOS, use 'xcrun --show-sdk-path' to get the SDK path and pass to clang by '-isysroot'
        if sys.platform == 'darwin':
            self._extra_args.append('-isysroot')
            self._extra_args.append(
                subprocess.run(['xcrun', '--show-sdk-path'], capture_output=True).stdout.decode('utf-8').strip())

    def _compile(self, source, output, include_dirs=(), library_dirs=(), extra_args=()):
        include_args = ('-I{}'.format(include) for include in (self._include_dirs + list(include_dirs)))
        library_args = ('-L{}'.format(library) for library in (self._library_dirs + list(library_dirs)))
        command = (self._binary, *include_args, *library_args, *extra_args, '-o', output, source)
        process = subprocess.run(command, capture_output=True)
        for output in (process.stdout.decode(), process.stderr.decode()):
            if 'error' in output or 'ERROR' in output:
                raise ValueError('clang outputs error message: {}'.format(output))

    def compile_binary(self, source, output, include_dirs=(), library_dirs=(), extra_args=()):
        logger.debug(f'Compiling {source} to binary using clang')
        self._compile(source, output, include_dirs, library_dirs, extra_args)

    def compile_bytecode(self, source, output, include_dirs=(), library_dirs=(), extra_args=()):
        logger.debug(f'Compiling {source} to bytecode using clang')
        extra_args = extra_args + ('-emit-llvm', '-g', '-c', '-O0')
        self._compile(source, output, include_dirs, library_dirs, extra_args)

    def preprocess(self, source, output, include_dirs=(), extra_args=()):
        extra_args = extra_args + (
            # ask clang to preprocess only
            '-E',
            # ask clang to not output linemarkers
            '-P'
        )
        self._compile(source, output, include_dirs, extra_args=extra_args)

    def syntax_check(self, source):
        extra_args = (
            # ask clang to only do syntax check
            '-fsyntax-only',
            # suppress warnings for annotation strings
            '-Wno-unused-value',
            # suppress warnings for Lap function calls
            '-Wno-implicit-function-declaration'
        )
        self._compile(source, '', extra_args=extra_args)


