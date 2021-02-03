from typing import List, Union, Optional, Dict, Sequence, Tuple
import asyncio
import pathlib
import shutil
import struct
import logging
import re
from checkdp.transform.typesystem import TypeSystem
import checkdp.transform.constants as constants
from checkdp.utils import InputType

logger = logging.getLogger(__name__)


class Z3:
    def __init__(self, z3_binary: str, output_dir: pathlib.Path = (pathlib.Path.cwd() / 'klee-out')):
        self._binary = pathlib.Path(z3_binary)

        # create output folder if not exists
        output_dir.mkdir(exist_ok=True)
        self._output_dir = output_dir

    async def async_solve(self, constraints: List[str], variables_length: Dict[str, int]):
        # print out the values for the symbolic variables
        constraints.append('(check-sat)')
        for variable, length in variables_length.items():
            for index in range(length):
                constraints.append(f'(get-value ((select {variable} (_ bv{index} 32))))')
        constraints.append('(exit)')

        smt2_file = (self._output_dir / 'minmax.smt2')
        with smt2_file.open('w') as fp:
            fp.write('\n'.join(constraints))

        process = await asyncio.subprocess.create_subprocess_exec(
            str(self._binary), str(smt2_file.resolve()), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        out, err = await process.communicate()
        await process.wait()

        # parse the output
        variable_bytes: Dict[str, bytearray] = {variable: bytearray(length) for variable, length in variables_length.items()}

        is_sat = out.decode('utf-8').split('\n', 1)[0]
        is_sat = True if is_sat == 'sat' else False

        for variable, byte_index, value in re.findall(
                r'\(\(\(select (\w+)\s\(_ bv(\d+) 32\)\) #x([0-9a-f]{2})\)\)', out.decode('utf-8')):
            variable_bytes[variable][int(byte_index)] = int(value, 16)

        # convert the bytearray into tuple of ints or int
        objects = {variable: None for variable in variable_bytes.keys()}
        for variable, byte_value in variable_bytes.items():
            value = tuple(element[0] for element in struct.iter_unpack('i', byte_value))
            objects[variable] = value
        return is_sat, objects

    def solve(self, constraints: List[str], variables_length: Dict[str, int]) \
            -> Tuple[bool, Dict[str, Union[int, Sequence[int]]]]:
        return asyncio.run(self.async_solve(constraints, variables_length))


class KLEE:
    def __init__(self, klee_binary: str, kleaver_binary: str, z3_obj: Z3,
                 output_dir: pathlib.Path = (pathlib.Path.cwd() / 'klee-out'),
                 backend: Union[str, Sequence[str]] = ('stp', 'z3'),
                 search_heuristic: str = 'dfs', show_output: bool = False):
        self._klee_binary = pathlib.Path(klee_binary)
        self._kleaver_binary = pathlib.Path(kleaver_binary)
        self._z3 = z3_obj
        self._output_dir = pathlib.Path(output_dir)
        # convert backend to a tuple
        self._backends = backend if isinstance(backend, (tuple, list)) else (backend,)
        self._search_heuristic = search_heuristic
        self._show_output = show_output

    async def _extract_constraints(self, kquery_file: Union[str, pathlib.Path]):
        process = await asyncio.create_subprocess_exec(
            str(self._kleaver_binary), '--print-smtlib', str(kquery_file),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await process.communicate()
        await process.wait()
        return out.decode('utf-8')

    async def _async_run(self, source: str, type_system: TypeSystem, is_maximize: bool) -> Optional[InputType]:
        # clear the output directory
        shutil.rmtree(self._output_dir, ignore_errors=True)
        self._output_dir.mkdir()

        # create processes (asyncio tasks) to run in different backends
        processes = {}
        for backend in self._backends:
            args = [
                '-exit-on-error-type=Assert', f'-output-dir={self._output_dir / backend}', '-use-cex-cache',
                f'--solver-backend={backend}', '-use-independent-solver', f'--search={self._search_heuristic}',
                source
            ]
            process = await asyncio.create_subprocess_exec(str(self._klee_binary), *args,
                                                           stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.PIPE)
            processes[asyncio.create_task(process.communicate())] = backend, process

        # wait for any of the process task (with corresponding backend) to finish, and then cancel the other tasks
        done, pending = await asyncio.wait(list(processes.keys()), return_when=asyncio.FIRST_COMPLETED)
        # since we use asyncio.FIRST_COMPLETED, done set will only contain one element
        done = done.pop()

        # first kill all other processes
        for task in pending:
            backend, process = processes[task]
            try:
                process.kill()
            except ProcessLookupError:
                """this is fine since the process might have already stopped"""
            await process.wait()

        # now extract the results from the finished process
        out, err = await done
        backend, process = processes[done]
        logger.debug(f'backend {backend} returned with a result')

        # KLEE outputs information to the stderr channel, therefore read everything from there
        content = out.decode('utf-8') + err.decode('utf-8')
        # check if KLEE doesn't finish properly
        if 'KLEE: done' not in content:
            raise ValueError(f'KLEE did not finish properly, full log:\n{content}')
        # check if KLEE returned an error that is not the desired ASSERTION FAIL
        for error_line in filter(lambda line: 'ERROR' in line and 'ASSERTION FAIL' not in line, content.splitlines()):
            raise ValueError(f'KLEE reported an error: {error_line}, full KLEE log: \n{content}')

        solver_output = self._output_dir / backend
        for file in solver_output.glob('*.assert.err'):
            # get kquery file and convert to smt2 format, we then solve the constraints on our own using z3
            # this gives us better flexibility to play with the generated constraints, such as minimizing / maximizing
            # the cost variables etc.
            kquery_file = solver_output / str(file).replace('.assert.err', '.kquery')

            # remove the last two lines: (check-sat) and (exit) since we will be appending other constraints
            constraints = (await self._extract_constraints(kquery_file)).splitlines()[:-2]

            with kquery_file.open('r') as fp:
                variable_length = {}
                for match in re.findall(r'array\s+([\w]+)\[(\d+)\]\s', fp.read()):
                    variable_length[match[0]] = int(match[1])

            # add minimize / maximize cost array constraint
            # since cost array is defined as an array of bitvectors, we first need to use `concat` to concatenate
            # every 4 bytes, and then use bvadd to add everything
            costs = []
            for byte_index in range(variable_length[constants.SYMBOLIC_COST] // 4):
                # use concat to form 4 bytes into one cost
                costs.append(
                    # here we prepend 8 bytes to prevent overflow when adding up later
                    f'(concat #x0000 (concat (concat (concat '
                    f'(select {constants.SYMBOLIC_COST} (_ bv{byte_index * 4} 32)) ' 
                    f'(select {constants.SYMBOLIC_COST} (_ bv{byte_index * 4 + 1} 32))) ' 
                    f'(select {constants.SYMBOLIC_COST} (_ bv{byte_index * 4 + 2} 32))) ' 
                    f'(select {constants.SYMBOLIC_COST} (_ bv{byte_index * 4 + 3} 32))))'
                )

            # then use bvadd to sum over all costs
            optimize_constraint = costs.pop()
            while len(costs) != 0:
                optimize_constraint = f'(bvadd {costs.pop()} {optimize_constraint})'
            keyword = 'maximize' if is_maximize else 'minimize'
            optimize_constraint = f"({keyword} {optimize_constraint})"
            constraints.append(optimize_constraint)

            # use z3 to solve the constraints and get the values
            is_sat, objects = await self._z3.async_solve(constraints, variable_length)

            if is_sat:
                # the objects dict returned from Z3 does not consider whether the variable is indeed an array or not
                # instead, it packs all values in to tuples
                # so we now look at the types in the type system, if the is_array is false, unpack the tuple
                for variable in objects.keys():
                    _, _, _, is_array = type_system.get_types(variable)
                    if not is_array:
                        if len(objects[variable]) != 1:
                            raise ValueError(f'{variable} is not registered as an array, but symbolic executor'
                                             f'returned an array value')
                        objects[variable] = objects[variable][0]
                return objects

        return None

    def run(self, source: str, type_system: TypeSystem, is_maximize: bool) -> Optional[InputType]:
        return asyncio.run(self._async_run(source, type_system, is_maximize))

    def reset(self):
        shutil.rmtree(self._output_dir)
