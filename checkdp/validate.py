from typing import Union, Sequence, Dict
import logging
import re
import subprocess
import os
from pathlib import Path
import sympy as sp
from checkdp.utils import InputType, OutputType

logger = logging.getLogger(__name__)


class PSI:
    def __init__(self, psi_binary: str, output_dir: Path):
        self._binary = psi_binary
        self._output_dir = output_dir
        self._return_finder = re.compile(r'return\s*(.*)\s*;')
        self._id_matcher = re.compile(r'^[_a-zA-Z][_a-zA-Z0-9]*$')

    def _preprocess(self, template: str) -> str:
        """preprocesses the template file, report any errors that are not consistent with our assumptions and then
        return the output variable name"""
        returns = self._return_finder.findall(template)
        if len(returns) > 1:
            raise NotImplementedError('Multiple return statement found, currently only supporting single return')
        if len(returns) == 0:
            raise ValueError('No return statement found')

        returned_variable = returns[0]
        if self._id_matcher.match(returned_variable) is None:
            raise NotImplementedError('Currently does not support returning expressions, '
                                      'please add it to a list and return the list instead')

        # check if the returned variable is indeed a list
        declaration = re.findall(returned_variable + r'\s*:=\s*(\(\[\s*\]\s*:\s*R\[\s*\]\));', template)
        if len(declaration) == 0:
            raise ValueError(f'Cannot find declaration for the output variable {returned_variable}')

        if declaration[0].replace(' ', '') != '([]:R[])':
            raise ValueError(f'Returned variable {returned_variable} is declared as ({declaration[0]}),'
                             f' instead of list ([]:R[]).')

        return returned_variable

    @staticmethod
    def concretize_probability(pdf: str, output_variable: str, bad_output: OutputType) -> float:
        # replace the variables in the pdf expression with concrete values of bad output
        for index, value in enumerate(bad_output):
            pdf = pdf.replace(f'{output_variable}{index}', str(value))
        # finally replace the length variable
        pdf = pdf.replace('length', str(len(bad_output)))

        logger.debug('Start evaluating...')
        # replace [ with ( and ] with ) since PSI uses [] to represent parentheses where PSI does not recognize
        probability = pdf.replace('[', '(').replace(']', ')')
        # use sympy to first cancel out the trivial parts
        probability = str(sp.cancel(probability))
        # replace the trivial values
        probability = probability.replace('Boole(True)', '1').replace('Boole(False)', '0').replace('DiracDelta(0)', '1')
        logger.debug('Final probability: {}'.format(probability))
        # now run sympy to simplify the final transformed expression, we should have a constant now
        return float(sp.simplify(probability).evalf())

    def validate(self, template: Union[str, os.PathLike],
                 inputs_1: InputType, inputs_2: InputType, bad_output: OutputType) -> Sequence[float]:
        # sanity checks for the inputs
        if set(inputs_1.keys()) != set(inputs_2.keys()):
            raise ValueError(f'Inputs 1 and Inputs 2 does not match, inputs_1: {inputs_1}, inputs_2: {inputs_2}')

        # find the name of the query variable by finding the different element between inputs_1 and inputs_2
        differences = tuple(filter(lambda p: p[1] != inputs_1[p[0]], inputs_2.items()))
        if len(differences) > 1:
            different_keys = tuple(difference[0] for difference in differences)
            different_inputs_1 = {key: inputs_1[key] for key in different_keys}
            different_inputs_2 = {key: inputs_2[key] for key in different_keys}
            raise ValueError(f'Inputs 1 and Inputs 2 differ too much, diff Inputs 1: {different_inputs_1},'
                             f' diff Inputs 2: {different_inputs_2}')
        query_variable = differences[0][0]

        # read the PSI template file
        with open(template, 'r') as f:
            template = f.read()

        returned_variable = self._preprocess(template)

        results = []
        for inputs in (inputs_1, inputs_2):
            # fill in the PSI template with concrete values from inputs and outputs
            content = str(template)
            for name, value in inputs.items():
                # convert tuple to list, since PSI doesn't support the format of tuple
                value = list(value) if isinstance(value, tuple) else value
                content = content.replace('${}$'.format(name), str(value))

            logger.debug('Evaluating bad output {}'.format(bad_output))

            # replace the array of output to separate element and a length
            content = self._return_finder.sub(
                f"return ({','.join(['{}[{}]'.format(returned_variable, i) for i in range(len(bad_output))])},"
                f"{returned_variable}.length);",
                content)

            input_sequence = '_'.join(map(str, inputs[query_variable]))
            output_sequence = '_'.join(map(str, bad_output))
            output_file = str((self._output_dir / f"psi_input_{input_sequence}_output_{output_sequence}.psi").resolve())
            # now write the filled template to a file to pass to psi
            with open(output_file, 'w') as f:
                f.write(content)

            # now run the psi process
            process = subprocess.run([self._binary, '--mathematica', '--raw', output_file], capture_output=True)
            err = process.stderr.decode('utf-8')
            if len(err) > 0:
                raise ValueError('PSI returned with error message {}'.format(err))
            pdf = process.stdout.decode('utf-8')

            logger.debug('The PDF of M({} \\in {}) is {}'.format(inputs[query_variable], bad_output, pdf))
            results.append(self.concretize_probability(pdf, returned_variable, bad_output))

        return results
