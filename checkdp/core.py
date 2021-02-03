from pathlib import Path
import stat
import logging
import subprocess

logger = logging.getLogger(__name__)


def run(template, klee, clang, output_dir=Path.cwd() / 'checkdp-out'):
    found_counterexamples = []
    possible_alignments = [template.default_alignment()]

    logger.info(f'Start by giving alignments {possible_alignments[0]}')
    suffix = 0
    iterations = 0

    is_find_inputs = True
    # final validate flag, when we cannot find an alignment that covers all
    is_final_validate = False
    # flag for jumping out of the regions using all previous alignments
    is_jump_out = False
    while True:
        search_object = 'inputs' if is_find_inputs else 'alignments'
        # first swap the symbolic and concretes
        concretes = possible_alignments if is_find_inputs else found_counterexamples

        concretes = [concretes[-1]] if is_final_validate and not is_jump_out else concretes

        logger.debug(f'Is final validate ? {is_final_validate}')
        logger.debug(f'Counterexamples({len(found_counterexamples)}): {found_counterexamples}')
        logger.debug(f'Possible alignments({len(possible_alignments)}): {possible_alignments}')

        logger.info(f"Now searching for {search_object} with {len(concretes)} concrete "
                    f"{'alignments' if is_find_inputs else 'inputs'}")

        content = '#define CHECKDP_KLEE\n' + template.fill(concretes, 5)

        concrete_file = output_dir / f'generate-{search_object}-{suffix}.c'
        bytecode_file = output_dir / f'generate-{search_object}-{suffix}.bc'

        with concrete_file.open('w') as f:
            f.write(content)

        # generate byte code using clang
        clang.compile_bytecode(str(concrete_file), str(bytecode_file))

        # run klee and get new set of concretes
        result = klee.run(str(bytecode_file), template.type_system(), is_maximize=is_find_inputs)

        # four possibilities:
        # (has_result, is_find_inputs) -> found a counterexample, append to list and go to next iteration
        # (no_result, is_find_inputs) -> cannot find counterexample, algorithm might be proven by alignment
        # (has_result, not_find_inputs) -> found an alignment that covers all previous counterexample
        # (no_result, not_find_inputs) -> the counterexample might be valid, send to further validation
        iterations += 1
        if result and is_find_inputs:
            logger.info(f'Found counterexample {result}')
            found_counterexamples.append(result)
            #iterations += 1
            # finished jumping out
            is_jump_out = False
        if not result and is_find_inputs:
            logger.info(r"Couldn't find input for the given alignment, the algorithm could possibly be proved "
                        f"by alignment {concretes[-1]}")
            logger.info('Now trying to refine the alignment')
            return True, concretes
        if result and not is_find_inputs:
            if is_final_validate:
                logger.warning('Counterexample does not pass final executor validation')
                logger.warning(f'Alignment {result} can prove it')
                possible_alignments.append(result)
                found_counterexamples = []
                is_final_validate = False
                is_jump_out = True
            else:
                logger.info(f'Found alignment {result}')
                # update the alignment since this alignment is better
                possible_alignments.pop()
                possible_alignments.append(result)
        if not result and not is_find_inputs:
            if is_final_validate:
                logger.info('Counterexample passes final executor validator')
                break
            else:
                logger.info('Cannot find an alignment, trying final executor validation')
                # we still need to add a final executor check
                # double flip input flag, so that next iteration we will still be searching for alignment
                is_final_validate = True
                is_find_inputs = not is_find_inputs
                suffix += 1000
        logger.info(f'The total iterations: {iterations}')
        # flip the find input flag
        is_find_inputs = not is_find_inputs

        # reset KLEE for next iteration
        klee.reset()

        suffix += 1

    # we have the final counterexample if we got here
    final_counterexample = found_counterexamples[-1]

    # now we try to get the bad outputs by replacing input with the concrete counterexample
    # and alignment with default null alignment, then run the binary using q and q + dq to get two bad outputs
    bad_outputs = []
    for index, bad_inputs in enumerate((final_counterexample, template.related_inputs(final_counterexample))):
        real_run_inputs = bad_inputs.copy()
        real_run_inputs.update(template.default_alignment())

        # remove the assertions, because we are supplying a default alignment which will trigger the
        # assertions if the counterexample is 'valid'
        content = '#define CHECKDP_REAL_RUN\n' + template.fill([real_run_inputs], 5, add_symbolic_cost=False)
        counterexample_file = output_dir / f'counterexample_badoutput_{index}.c'
        with counterexample_file.open('w') as f:
            f.write(content)

        # compile the source file to binary to be executed
        binary_file = output_dir / f'badoutput_{index}'
        clang.compile_binary(counterexample_file, binary_file)

        # run the binary and get the output
        # add executable permission
        binary_file.chmod(binary_file.stat().st_mode | stat.S_IEXEC)
        process = subprocess.run(str(binary_file.resolve()), capture_output=True)
        bad_outputs.append(process.stdout.decode('utf-8').splitlines())
        logger.debug(f"Violation line in transformed file: {process.stderr.decode('utf-8').splitlines()}")

    return False, final_counterexample, bad_outputs
