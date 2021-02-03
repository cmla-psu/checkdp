import pytest
from pathlib import Path
from checkdp.__main__ import main
from tests.utils import example_folder

algorithms = tuple(tuple(example_folder.glob('*.c')))


@pytest.mark.parametrize('algorithm', algorithms, ids=[algorithm.stem for algorithm in algorithms])
def test_algorithms(algorithm: Path, result_folder: Path):
    result = result_folder / algorithm.stem
    args = [str(algorithm)]
    # add shadow execution to noisy max and its variants
    if 'noisymax' in str(algorithm):
        args.append('--enable-shadow')
    # PSI validation for badadaptivesvt is too expensive therefore we temporarily ignore it
    if algorithm.name.startswith('bad') and 'adaptive' not in algorithm.name:
        args.append('-s')
        args.append(str(algorithm.with_suffix('.psi')))
    args.append('-o')
    args.append(str(result))
    # run the main procedure
    main(args)
    log_file = result / 'run.log'
    # copy the results to the result folder if keep is specified
    with log_file.open('r') as f:
        # check the log for final result
        if algorithm.name.startswith('bad'):
            assert 'Result: Counterexample Found' in f.read()
        else:
            assert 'Result: Alignment Found' in f.read()


