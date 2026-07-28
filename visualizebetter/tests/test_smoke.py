from typer.testing import CliRunner

import visualizebetter
from visualizebetter.cli import app


def test_package_imports():
    assert visualizebetter.__version__


def test_cli_version_command():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert visualizebetter.__version__ in result.stdout
