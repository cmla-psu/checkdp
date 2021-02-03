import pytest
import shutil
import tempfile
from pathlib import Path
from tests.utils import root_folder


def pytest_addoption(parser):
    parser.addoption('--keep', action='store_true')


@pytest.fixture(scope='session')
def keep(request):
    is_keep = request.config.option.keep
    return False if is_keep is None else is_keep


@pytest.fixture(scope='session')
def result_folder(keep: bool):
    if keep:
        result = root_folder / 'checkdp-results'
        if result.exists():
            shutil.rmtree(result)
        result.mkdir()
        yield result
    else:
        with tempfile.TemporaryDirectory() as tempdir:
            yield Path(tempdir)

