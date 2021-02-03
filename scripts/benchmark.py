import asyncio
import pathlib
import os
import re
import sys
import shutil
import time
import argparse
import tqdm


def ellipsize(string, max_len=50):
    return string if len(string) <= max_len else string[:max_len - 3] + '...'


def success(message):
    return '\033[92m{}\033[0m'.format(message)


def fail(message):
    return '\033[91m{}\033[0m'.format(message)


async def check(file_path, output_folder):
    args = ['-m', 'checkdp', str(file_path)]
    if 'noisymax' in file_path.name:
        args.append('--enable-shadow')
    args.append('-o')
    args.append(str(output_folder))
    process = await asyncio.create_subprocess_exec(
        sys.executable, *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await process.communicate()
    await process.wait()
    return file_path.stem, out.decode('utf-8'), err.decode('utf-8')


async def main(argv=sys.argv[1:]):
    # parse the arguments
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument('--parallel', action='store_true', dest='parallel',
                            default=False, required=False,
                            help='Run the benchmark tests in parallel. '
                                 'NOTE: the running time for each task might be slightly affected.')
    arguments = arg_parser.parse_args(argv)

    module_folder = pathlib.Path(__file__).resolve().parents[1]

    # remove existing results folder and create a new one
    result_folder = module_folder / 'results'
    if result_folder.exists():
        print('Found previous results folder, removing..')
        shutil.rmtree(result_folder)
        print('Previous results folder removed.')
    os.mkdir(result_folder)

    # find out all source files ending with .c in the example folder
    example_folder = module_folder / 'examples'
    source_files = [(example_folder / file).resolve()
                    for file in filter(lambda item: (example_folder / item).is_file(), example_folder.glob('*.c'))]

    # set PYTHONPATH environment in order for subprocesses to find the module checkdp
    os.environ['PYTHONPATH'] = '{}:{}'.format(os.environ['PYTHONPATH'], str(module_folder)) \
        if 'PYTHONPATH' in os.environ else str(module_folder)

    # create the coroutine map (name -> coroutine)
    coroutines = {file.stem: check(file, result_folder / file.stem) for file in source_files}

    # create progress bar, the description indicates the remaining examples
    task_generator = (asyncio.ensure_future(coro) for coro in coroutines.values())
    if arguments.parallel:
        # if parallel argument is specified, create all tasks now and feed it into as_completed wrapper
        print('--parallel argument detected, running all examples in parallel, '
              'note that the running time for each task might be affected.')
        task_generator = asyncio.as_completed(tuple(task_generator))
    bar = tqdm.tqdm(task_generator,
                    total=len(coroutines), desc='[{}]'.format(ellipsize(' '.join(coroutines.keys()))), unit='example')

    start_time = time.time()
    # for showing the remaining examples in the progress bar description, we use a finished set to track
    # those finished examples
    finished = set()
    error_check = re.compile(r'error|exception', re.IGNORECASE)
    for task in bar:
        # periodically wake up from "sleep 1 second" task to refresh the progress bar so we have a precise time tracking
        while True:
            sleep_task = asyncio.ensure_future(asyncio.sleep(1))
            done, pending = await asyncio.wait([task, sleep_task], return_when=asyncio.FIRST_COMPLETED)
            bar.refresh()
            if sleep_task in done:
                # woke up from sleep task, re-assign the actual task to be awaited in next iteration
                task = pending.pop()
            else:
                # woke up from the actual task, therefore cancel the sleep task and break the loop
                pending.pop().cancel()
                task = done.pop()
                break
        # get the example name from the task
        name, out, err = await task
        finished.add(name)
        for output in (out, err):
            if error_check.search(output) is not None:
                print(output)

        # check if the results are consistent with the problem name
        with open(result_folder / name / 'run.log', 'r') as f:
            content = f.read()
            is_ok = ('Result: Alignment Found' in content and not name.startswith('bad')) or \
                ('Result: Counterexample Found' in content and name.startswith('bad'))

        # log the message and update progress bar description
        bar.write('Finished checking {} in {:0.1f} seconds, reports can be found at {}, result: {}'.format(
            name, time.time() - start_time, str(result_folder / name), success('ok') if is_ok else fail('not ok')))
        remaining = tuple(filter(lambda x: x not in finished, coroutines.keys()))
        if len(remaining) == 0:
            bar.set_description('Done')
        else:
            bar.set_description('[{}]'.format(ellipsize(' '.join(remaining))))


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
