from typing import Dict, Union, Iterable, Callable, Sequence, List
import itertools
import re
import sympy as sp
import pycparser.c_ast as c_ast
import checkdp.transform.constants as constants
from checkdp.transform.typesystem import TypeSystem
from checkdp.transform.random_distance import AlignmentIndexType
from checkdp.transform.utils import generate, parse

_Number = Union[int, float]


_HEADER = f"""#include <stdio.h>
#include <assert.h>

#ifdef {constants.PREFIX}_KLEE
    #include <klee/klee.h>
    void __assert_fail(const char * assertion, const char * file, unsigned int line, const char * function)
    {{
        abort();
    }}
    #define {constants.ASSERT}(cond) if (!(cond)) {{ return (PENALTY + 1); }}
    #define {constants.OUTPUT}(var) {{}}
    #define {constants.ASSUME}(cond) klee_assume(cond)
#endif

#ifdef {constants.PREFIX}_REAL_RUN
    #define {constants.ASSERT}(cond) {{if (!(cond)) {{ fprintf(stderr, "%d", __LINE__); }}}}
    #define {constants.OUTPUT}(var) fprintf(stdout, "%d\\n", (var));
    #define {constants.ASSUME}(cond) {{}}
#endif

#define Abs(x) ((x) < 0 ? -(x) : (x))
"""


def _simplify_node(node: c_ast.Node):
    def translate(expression: str, encode):
        transcodes = {
            '[': '__LEFTBRACE__',
            ']': '__RIGHTBRACE__'
        }
        for k, v in transcodes.items():
            expression = expression.replace(k, v) if encode else expression.replace(v, k)
        return expression

    if isinstance(node, c_ast.TernaryOp):
        return f'({generate(node.cond)}) ? ({_simplify_node(node.iftrue)}) : ({_simplify_node(node.iffalse)})'
    elif isinstance(node, c_ast.BinaryOp) and node.op in ('&&', '||'):
        return f'({_simplify_node(node.left)}) {node.op} ({_simplify_node(node.right)})'
    else:
        return translate(str(sp.simplify(translate(generate(node), encode=True))), encode=False)


class Template:
    """Template class holds pieces of transformed program with holes for inputs and alignments. When fill_inputs /
    fill_alignments method is called, it fills in the corresponding concrete part and make the other parts symbolic,
    then returns the final transformed program with driver code (main function) that is runnable by symbolic execution
    engines."""
    def __init__(self, type_system: TypeSystem, function: c_ast.FuncDef, random_distances: str,
                 goal: str, alignment_array_types: List[AlignmentIndexType], sample_array_size_func: Callable[[int], int],
                 preconditions: Iterable[str], holes: Iterable[str], hole_preconditions: Iterable[str]):
        self._type_system = type_system
        self._function = function
        self._parameters = tuple(decl.name for decl in function.decl.type.args.params)
        self._random_distances = random_distances
        self._goal = goal
        self._alignment_array_types = alignment_array_types
        self._sample_array_size_func = sample_array_size_func
        self._preconditions = preconditions
        self._holes = holes
        self._hole_preconditions = hole_preconditions

    def __str__(self):
        # give an empty alignment for debugging purposes
        return self.fill([{constants.ALIGNMENT_ARRAY: [0 for _ in range(len(self._alignment_array_types))]}], 5)

    def default_alignment(self) -> Dict[str, Union[_Number, Sequence[_Number]]]:
        return {constants.ALIGNMENT_ARRAY: [0 for _ in range(len(self._alignment_array_types))]}

    def fill_default(self, query_size: int):
        return self.fill([self.default_alignment()], query_size)

    def type_system(self) -> TypeSystem:
        return self._type_system

    def random_distance(self, alignments_values):
        alignments_values = alignments_values[-1][constants.ALIGNMENT_ARRAY]
        alignments = {}
        for match in re.finditer(f'#define\\s+{constants.RANDOM_DISTANCE}_([a-zA-Z_][a-zA-Z0-9_]+)\\s+(.*)',
                                 self._random_distances):
            alignment = match.group(2)
            for index, value in enumerate(alignments_values):
                alignment = alignment.replace(f'{constants.ALIGNMENT_ARRAY}[{index}]', str(value))
            # try to simplify the expression (mostly eliminating the terms with coefficient 0)
            try:
                alignment = _simplify_node(parse(alignment))
            finally:
                alignments[match.group(1)] = alignment
        return alignments

    def selector(self, alignments_values):
        alignments_values = alignments_values[-1][constants.ALIGNMENT_ARRAY]
        alignments = {}
        for match in re.finditer(f'#define\\s+{constants.SELECTOR}_([a-zA-Z_][a-zA-Z0-9_]+)\\s+(.*)',
                                 self._random_distances):
            alignment = match.group(2)
            for index, value in enumerate(alignments_values):
                value = 'ALIGNED' if str(value) == constants.SELECT_ALIGNED else 'SHADOW'
                alignment = alignment.replace(f'{constants.ALIGNMENT_ARRAY}[{index}]', str(value))
            alignments[match.group(1)] = alignment
        return alignments

    def related_inputs(self, original_inputs: Dict[str, Union[_Number, Sequence[_Number]]]):
        query_variable = self._function.decl.type.args.params[0].name
        related = original_inputs.copy()
        related[query_variable] = [x + y for x, y in zip(related[query_variable],
                                                         related[f'{constants.ALIGNED_DISTANCE}_{query_variable}'])]
        return related

    def fill(self, concretes: Sequence[Dict[str, Union[_Number, Sequence[_Number]]]], query_size: int,
             add_symbolic_cost: bool = True):
        """Generate driver code (main function) and return the whole transformed program"""
        if len(concretes) == 0 or len(concretes[0]) == 0:
            raise NotImplementedError('At least one concrete must be provided to start the process')
        # TODO: add sanity checks for parameter concretes

        sample_array_size = self._sample_array_size_func(query_size)
        query_node, size_node, epsilon_node, *other_parameters = (decl for decl in self._function.decl.type.args.params)
        user_parameters = [param for param in other_parameters if not param.name.startswith(constants.PREFIX)]
        added_parameters = [param for param in other_parameters if param.name.startswith(constants.PREFIX)]
        query_assumption, *user_assumptions = self._preconditions

        # add declarations of all the variables
        query = query_node.name
        declarations = [
            # declarations of variables
            f'int {query}[{query_size}];',
            f'int {epsilon_node.name} = 1;',
            *(f'int {param.name};' if isinstance(param.type, c_ast.TypeDecl) else f'int {param.name}[{query_size}];'
              for param in user_parameters),
            # for symbolic cost variables
            f'int {constants.SYMBOLIC_COST}[{len(concretes)}];'
        ]

        for param in added_parameters:
            if param.name.startswith(constants.ALIGNED_DISTANCE) or param.name.startswith(constants.SHADOW_DISTANCE):
                declarations.append(f'int {param.name}[{query_size}];')
            elif param.name.startswith(constants.SAMPLE_ARRAY):
                declarations.append(f'int {param.name}[{sample_array_size}];')
            elif param.name.startswith(constants.ALIGNMENT_ARRAY):
                declarations.append(f'int {param.name}[{len(self._alignment_array_types)}];')
            elif param.name.startswith(constants.HOLE):
                declarations.append(f'int {param.name};')
            else:
                raise NotImplementedError(f'Unknown generated parameter: {param}')

        # first create assumptions based on the concretes
        assumptions = []
        has_inputs = query in concretes[0]
        has_alignments = constants.ALIGNMENT_ARRAY in concretes[0]
        operator = '<'
        # we're given concrete inputs, now looking for alignments
        if has_inputs and not has_alignments:
            selector_indices = tuple(
                index for index, value in enumerate(self._alignment_array_types) if value == AlignmentIndexType.Selector
            )
            selector_expression = ' || '.join(f'i == {index}' for index in selector_indices) if len(selector_indices) != 0 else '0'
            assumptions.append(
                f'for(int i = 0; i < {len(self._alignment_array_types)}; i ++)' '{\n'
                f"  if({selector_expression})" '{\n'
                f'    {constants.ASSUME}({constants.ALIGNMENT_ARRAY}[i] >= {constants.SELECT_ALIGNED});\n'
                f'    {constants.ASSUME}({constants.ALIGNMENT_ARRAY}[i] <= {constants.SELECT_SHADOW});\n'
                '   } else {\n'
                f'    {constants.ASSUME}({constants.ALIGNMENT_ARRAY}[i] <= 4);\n'
                f'    {constants.ASSUME}({constants.ALIGNMENT_ARRAY}[i] >= -4);\n'
                '   }\n'
                f'  klee_prefer_cex({constants.ALIGNMENT_ARRAY}, {constants.ALIGNMENT_ARRAY}[i] == 0);\n'
                '}\n'
            )
            # user assumptions for custom holes
            for condition in self._hole_preconditions:
                assumptions.append(
                    f'{constants.ASSUME}({condition});'
                )
            operator = '<='
        if has_alignments and not has_inputs:
            if query_assumption == constants.ALL_DIFFER:
                assumptions.append(
                    f'for(int i = 0; i < {query_size}; i ++)' '{\n'
                    f'  {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] >= -1);\n'
                    f'  {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] <= 1);\n'
                    f'  {constants.ASSUME}({query}[i] >= -10);\n'
                    f'  {constants.ASSUME}({query}[i] <= 10);\n'
                    f'  klee_prefer_cex({constants.ALIGNED_DISTANCE}_{query}, {constants.ALIGNED_DISTANCE}_{query}[i] != 0);\n'
                    '}\n'
                )
            elif query_assumption == constants.ONE_DIFFER:
                assumptions.append(
                    f'{constants.ASSUME}({constants.PREFIX}_index >= 0);\n'
                    f'{constants.ASSUME}({constants.PREFIX}_index < {query_size});\n'
                    f'for(int i = 0; i < {query_size}; i ++)' '{\n'
                    f'  {constants.ASSUME}({query}[i] >= -10);\n'
                    f'  {constants.ASSUME}({query}[i] <= 10);\n'
                    f'  if({constants.PREFIX}_index == i) ' '{\n'
                    f'    {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] >= -1);\n'
                    f'    {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] <= 1);\n'
                    f'    klee_prefer_cex({constants.ALIGNED_DISTANCE}_{query}, {constants.ALIGNED_DISTANCE}_{query}[i] != 0);\n'
                    '  } else {\n'
                    f'    {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] == 0);\n'
                    '  }\n'
                    '}\n'
                )
            elif query_assumption == constants.DECREASING:
                assumptions.append(
                    f'for(int i = 0; i < {query_size}; i ++)' '{\n'
                    f'  {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] >= -1);\n'
                    f'  {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] <= 0);\n'
                    f'  {constants.ASSUME}({query}[i] >= -10);\n'
                    f'  {constants.ASSUME}({query}[i] <= 10);\n'
                    f'  klee_prefer_cex({constants.ALIGNED_DISTANCE}_{query}, {constants.ALIGNED_DISTANCE}_{query}[i] != 0);\n'
                    '}\n'
                )
            elif query_assumption == constants.INCREASING:
                assumptions.append(
                    f'for(int i = 0; i < {query_size}; i ++)' '{\n'
                    f'  {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] >= 0);\n'
                    f'  {constants.ASSUME}({constants.ALIGNED_DISTANCE}_{query}[i] <= 1);\n'
                    f'  {constants.ASSUME}({query}[i] >= -10);\n'
                    f'  {constants.ASSUME}({query}[i] <= 10);\n'
                    f'  klee_prefer_cex({constants.ALIGNED_DISTANCE}_{query}, {constants.ALIGNED_DISTANCE}_{query}[i] != 0);\n'
                    '}\n'
                )
            else:
                raise NotImplementedError(f'Query assumption {query_assumption} not yet supported.')
            # assumptions about the sample array
            assumptions.append(
                f'for(int i = 0; i < {sample_array_size}; i++) ' '{\n'
                f'  {constants.ASSUME}({constants.SAMPLE_ARRAY}[i] >= -10);\n'
                f'  {constants.ASSUME}({constants.SAMPLE_ARRAY}[i] <= 10);\n'
                '}\n'
            )
            # user-defined assumptions
            for assumption in user_assumptions:
                if size_node.name in assumption:
                    # TODO: use regex for better robustness
                    assumptions.append(f'{constants.ASSUME}({assumption.replace(size_node.name, str(query_size))});')
                else:
                    assumptions.append(f'{constants.ASSUME}({assumption});')
            operator = '>'

        # create symbolic variables
        symbolic_statements = []
        if add_symbolic_cost:
            symbolic_statements.append(
                # first make the symbolic cost variables symbolic
                f'klee_make_symbolic({constants.SYMBOLIC_COST}, sizeof({constants.SYMBOLIC_COST}), '
                f'\"{constants.SYMBOLIC_COST}\");'
            )
            
        if query_assumption == constants.ONE_DIFFER:
            declarations.append(f'int {constants.PREFIX}_index;')
            self._type_system.update_base_type(f'{constants.PREFIX}_index', 'int', False)
        if has_alignments and not has_inputs and query_assumption == constants.ONE_DIFFER:
            symbolic_statements.append(
                f'klee_make_symbolic(&{constants.PREFIX}_index, sizeof({constants.PREFIX}_index), '
                f'\"{constants.PREFIX}_index\");'
            )
        for variable in {decl.name for decl in self._function.decl.type.args.params}.difference(concretes[0].keys()):
            if variable == constants.ALIGNMENT_ARRAY:
                symbolic_statements.append(
                    f'klee_make_symbolic({constants.ALIGNMENT_ARRAY}, sizeof({constants.ALIGNMENT_ARRAY}), '
                    f'\"{constants.ALIGNMENT_ARRAY}\");'
                )
            elif variable == epsilon_node.name or variable == size_node.name:
                # epsilon is pre-specified as 1 and size variable is controlled by query_size
                continue
            elif constants.ALIGNED_DISTANCE in variable:
                if variable != f'{constants.ALIGNED_DISTANCE}_{query_node.name}':
                    raise NotImplementedError(f'Support for non-query arrays is not implemented yet: {variable}')
                symbolic_statements.append(
                    f'klee_make_symbolic({variable}, sizeof({variable}), \"{variable}\");'
                )

            elif constants.SAMPLE_ARRAY in variable:
                symbolic_statements.append(
                    f'klee_make_symbolic({constants.SAMPLE_ARRAY}, sizeof({constants.SAMPLE_ARRAY}), '
                    f'\"{constants.SAMPLE_ARRAY}\");'
                )
            elif constants.HOLE in variable:
                symbolic_statements.append(
                    f'klee_make_symbolic(&{variable}, sizeof({variable}), \"{variable}\");'
                )
            else:
                _, _, _, is_array = self._type_system.get_types(variable)
                variable_pointer = f'{variable}' if is_array else f'&{variable}'
                symbolic_statements.append(
                    f'klee_make_symbolic({variable_pointer}, sizeof({variable}), \"{variable}\");'
                )

        # create function calls
        parameter_list = ', '.join(decl.name if decl != size_node else str(query_size)
                                   for decl in self._function.decl.type.args.params)
        function_call = f"{self._function.decl.name}({parameter_list})"
        function_calls = []
        for initializer_index, initializer in enumerate(concretes):
            initialize_statements = []
            for variable, value in initializer.items():
                if variable == constants.SYMBOLIC_COST:
                    continue
                if isinstance(value, (tuple, list)):
                    for index, item_value in enumerate(value):
                        initialize_statements.append(f'{variable}[{index}] = {item_value};')
                else:
                    initialize_statements.append(f'{variable} = {value};')
            # function call
            function_calls.append('  ' * initializer_index + ' '.join(initialize_statements))
            function_calls.append('  ' * initializer_index + f'int {constants.PREFIX}_cost_{initializer_index} = {function_call};')
            # TODO: simplify the following code, it tries to ask for a proof that makes K <= v_epsilon <= epsilon
            #if has_inputs:
            function_calls.append('  ' * initializer_index + f'if ({constants.PREFIX}_cost_{initializer_index} {operator} {self._goal})')
            #else:
                #function_calls.append('  ' * initializer_index + f'if ({constants.PREFIX}_cost_{initializer_index} {operator} {self._goal})')
            function_calls.append('  ' * initializer_index + '{')
            # add the final assert
            # if everything is specified (all inputs and alignment array), we should not generate klee_assert statement
            if not (has_inputs and has_alignments) and initializer_index == len(concretes) - 1:
                symbolic_costs_equal_cost = \
                    ' && '.join(
                        f"{constants.PREFIX}_cost_{index} == {constants.SYMBOLIC_COST}[{index}]"
                        for index in range(len(concretes))
                    )
                function_calls.append('  ' * (initializer_index + 1) + f"if ({symbolic_costs_equal_cost})")
                function_calls.append('  ' * (initializer_index + 1) + '{')
                function_calls.append('  ' * (initializer_index + 2) + f'klee_assert(0);')
                function_calls.append('  ' * (initializer_index + 1) + '}')

        # add closing brackets
        for initializer_index in range(len(concretes), 0, -1):
            function_calls.append('  ' * (initializer_index - 1) + '}')

        # create main body
        main_body = '\n'.join(itertools.chain(declarations, symbolic_statements, assumptions, function_calls))
        # add indentation for main function
        main_body = '\n'.join('  ' + statement for statement in main_body.splitlines())
        header = _HEADER.replace('PENALTY', self._goal)
        # TODO: temporarily replace all floats with ints
        function = generate(self._function).replace('float', 'int')
        # TODO: temporarily replace CHECKDP_SHADOW_DISTANCE_q with CHECKDP_ALIGNED_DISTANCE_q
        function = function.replace(f'{constants.SHADOW_DISTANCE}_{query_node.name}',
                                    f'{constants.ALIGNED_DISTANCE}_{query_node.name}')
        return f'{header}\n{self._random_distances}\n\n{function}\n\nint main(void) {{\n{main_body}\n}}'
